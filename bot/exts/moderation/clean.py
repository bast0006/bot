import logging
import re
import time
from collections import defaultdict
from typing import Any, Callable, DefaultDict, Iterable, List, Literal, Optional, TYPE_CHECKING, Tuple, Union

from discord import Colour, Embed, Message, NotFound, TextChannel, User, errors
from discord.ext.commands import Cog, Context, Converter, group, has_any_role
from discord.ext.commands.converter import TextChannelConverter
from discord.ext.commands.errors import BadArgument, MaxConcurrencyReached, MissingRequiredArgument

from bot.bot import Bot
from bot.constants import (
    Channels, CleanMessages, Colours, Emojis, Event, Icons, MODERATION_ROLES
)
from bot.exts.moderation.modlog import ModLog
from bot.utils.channel import is_mod_channel

log = logging.getLogger(__name__)

# Type alias for checks
Predicate = Callable[[Message], bool]


class CleanChannels(Converter):
    """A converter that turns the given string to a list of channels to clean, or the literal `*` for all channels."""

    _channel_converter = TextChannelConverter()

    async def convert(self, ctx: Context, argument: str) -> Union[Literal["*"], list[TextChannel]]:
        """Converts a string to a list of channels to clean, or the literal `*` for all channels."""
        if argument == "*":
            return "*"
        return [await self._channel_converter.convert(ctx, channel) for channel in argument.split()]


if TYPE_CHECKING:
    CleanChannels = Union[Literal["*"], list[TextChannel]]  # noqa: F811


class Clean(Cog):
    """
    A cog that allows messages to be deleted in bulk, while applying various filters.

    You can delete messages sent by a specific user, messages sent by bots, all messages, or messages that match a
    specific regular expression.

    The deleted messages are saved and uploaded to the database via an API endpoint, and a URL is returned which can be
    used to view the messages in the Discord dark theme style.
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.cleaning = False

    @property
    def mod_log(self) -> ModLog:
        """Get currently loaded ModLog cog instance."""
        return self.bot.get_cog("ModLog")

    # region: Helper functions

    @staticmethod
    def _validate_input(
            traverse: int,
            channels: CleanChannels,
            bots_only: bool,
            user: User,
            until_message: Message,
            after_message: Message,
            use_cache: bool
    ) -> None:
        """Raise errors if an argument value or a combination of values is invalid."""
        # Is this an acceptable amount of messages to traverse?
        if traverse > CleanMessages.message_limit:
            raise BadArgument(f"You cannot traverse more than {CleanMessages.message_limit} messages.")

        if after_message:
            # Ensure that until_message is specified.
            if not until_message:
                raise MissingRequiredArgument("`until_message` must be specified if `after_message` is specified.")

            # Messages are not in same channel
            if after_message.channel != until_message.channel:
                raise BadArgument("You cannot do range clean across several channel.")

            # Ensure that after_message is younger than until_message
            if after_message.created_at >= until_message.created_at:
                raise BadArgument("`after` message must be younger than `until` message")

    def _get_messages_from_cache(self, traverse: int, to_delete: Predicate) -> Tuple[DefaultDict, List[int]]:
        """Helper function for getting messages from the cache."""
        message_mappings = defaultdict(list)
        message_ids = []
        for message in self.bot.cached_messages:
            if not self.cleaning:
                # Cleaning was canceled
                return (message_mappings, message_ids)

            if to_delete(message):
                message_mappings[message.channel].append(message)
                message_ids.append(message.id)

                if len(message_ids) == traverse:
                    # We traversed the requested amount of messages.
                    return message_mappings, message_ids

        # There are fewer messages in the cache than the number requested to traverse.
        return message_mappings, message_ids

    async def _get_messages_from_channels(
        self,
        traverse: int,
        channels: Iterable[TextChannel],
        to_delete: Predicate,
        until_message: Optional[Message] = None
    ) -> tuple[defaultdict[Any, list], list]:
        message_mappings = defaultdict(list)
        message_ids = []

        for channel in channels:

            async for message in channel.history(limit=traverse):

                if not self.cleaning:
                    # Cleaning was canceled, return empty containers
                    return defaultdict(list), []

                if until_message:

                    # We could use ID's here however in case if the message we are looking for gets deleted,
                    # We won't have a way to figure that out thus checking for datetime should be more reliable
                    if message.created_at < until_message.created_at:
                        # Means we have found the message until which we were supposed to be deleting.
                        break

                if to_delete(message):
                    message_mappings[message.channel].append(message)
                    message_ids.append(message.id)

        return message_mappings, message_ids

    @staticmethod
    def is_older_than_14d(message: Message) -> bool:
        """
        Precisely checks if message is older than 14 days, bulk deletion limit.

        Inspired by how purge works internally.
        Comparison on message age could possibly be less accurate which in turn would resort in problems
        with message deletion if said messages are very close to the 14d mark.
        """
        two_weeks_old_snowflake = int((time.time() - 14 * 24 * 60 * 60) * 1000.0 - 1420070400000) << 22
        return message.id < two_weeks_old_snowflake

    async def _delete_messages_individually(self, messages: List[Message]) -> list[Message]:
        """Delete each message in the list unless cleaning is cancelled. Return the deleted messages."""
        deleted = []
        for message in messages:
            # Ensure that deletion was not canceled
            if not self.cleaning:
                return deleted
            try:
                await message.delete()
            except NotFound:
                # Message doesn't exist or was already deleted
                continue
            else:
                deleted.append(message)
        return deleted

    async def _delete_found(self, message_mappings: dict[TextChannel, list[Message]]) -> list[Message]:
        """
        Delete the detected messages.

        Deletion is made in bulk per channel for messages less than 14d old.
        The function returns the deleted messages.
        If cleaning was cancelled in the middle, return messages already deleted.
        """
        deleted = []
        for channel, messages in message_mappings.items():
            to_delete = []

            for current_index, message in enumerate(messages):
                if not self.cleaning:
                    # Means that the cleaning was canceled
                    return deleted

                if self.is_older_than_14d(message):
                    # further messages are too old to be deleted in bulk
                    deleted_remaining = await self._delete_messages_individually(messages[current_index:])
                    deleted.extend(deleted_remaining)
                    if not self.cleaning:
                        # Means that deletion was canceled while deleting the individual messages
                        return deleted
                    break

                to_delete.append(message)

                if len(to_delete) == 100:
                    # we can only delete up to 100 messages in a bulk
                    await channel.delete_messages(to_delete)
                    deleted.extend(to_delete)
                    to_delete.clear()

            if len(to_delete) > 0:
                # deleting any leftover messages if there are any
                await channel.delete_messages(to_delete)
                deleted.extend(to_delete)

        return deleted

    async def _log_clean(self, messages: list[Message], channels: CleanChannels, invoker: User) -> None:
        """Log the deleted messages to the modlog."""
        if not messages:
            # Can't build an embed, nothing to clean!
            raise BadArgument("No matching messages could be found.")

        # Reverse the list to have reverse chronological order
        log_messages = reversed(messages)
        log_url = await self.mod_log.upload_log(log_messages, invoker.id)

        # Build the embed and send it
        if channels == "*":
            target_channels = "all channels"
        else:
            target_channels = ", ".join(channel.mention for channel in channels)

        message = (
            f"**{len(messages)}** messages deleted in {target_channels} by "
            f"{invoker.mention}\n\n"
            f"A log of the deleted messages can be found [here]({log_url})."
        )

        await self.mod_log.send_log_message(
            icon_url=Icons.message_bulk_delete,
            colour=Colour(Colours.soft_red),
            title="Bulk message delete",
            text=message,
            channel_id=Channels.mod_log,
        )

    # endregion

    async def _clean_messages(
        self,
        traverse: int,
        ctx: Context,
        channels: CleanChannels,
        bots_only: bool = False,
        user: User = None,
        regex: Optional[str] = None,
        until_message: Optional[Message] = None,
        after_message: Optional[Message] = None,
        use_cache: Optional[bool] = True
    ) -> None:
        """A helper function that does the actual message cleaning."""
        def predicate_bots_only(message: Message) -> bool:
            """Return True if the message was sent by a bot."""
            return message.author.bot

        def predicate_specific_user(message: Message) -> bool:
            """Return True if the message was sent by the user provided in the _clean_messages call."""
            return message.author == user

        def predicate_regex(message: Message) -> bool:
            """Check if the regex provided in _clean_messages matches the message content or any embed attributes."""
            content = [message.content]

            # Add the content for all embed attributes
            for embed in message.embeds:
                content.append(embed.title)
                content.append(embed.description)
                content.append(embed.footer.text)
                content.append(embed.author.name)
                for field in embed.fields:
                    content.append(field.name)
                    content.append(field.value)

            # Get rid of empty attributes and turn it into a string
            content = [attr for attr in content if attr]
            content = "\n".join(content)

            # Now let's see if there's a regex match
            if not content:
                return False
            else:
                return bool(re.search(regex.lower(), content.lower()))

        def predicate_range(message: Message) -> bool:
            """Check if message is older than message provided in after_message but younger than until_message."""
            return after_message.created_at <= message.created_at <= until_message.created_at

        self._validate_input(traverse, channels, bots_only, user, until_message, after_message, use_cache)

        # Are we already performing a clean?
        if self.cleaning:
            raise MaxConcurrencyReached("Please wait for the currently ongoing clean operation to complete.")
        self.cleaning = True

        # Set up the correct predicate
        if bots_only:
            predicate = predicate_bots_only      # Delete messages from bots
        elif user:
            predicate = predicate_specific_user  # Delete messages from specific user
        elif regex:
            predicate = predicate_regex          # Delete messages that match regex
        elif after_message:
            predicate = predicate_range          # Delete messages older than specific message
        else:
            predicate = lambda m: True           # Delete all messages  # noqa: E731

        # Default to using the invoking context's channel
        if not channels:
            channels = [ctx.channel]

        if not is_mod_channel(ctx.channel):

            # Delete the invocation first
            self.mod_log.ignore(Event.message_delete, ctx.message.id)
            try:
                await ctx.message.delete()
            except errors.NotFound:
                # Invocation message has already been deleted
                log.info("Tried to delete invocation message, but it was already deleted.")

        if channels == "*" and use_cache:
            message_mappings, message_ids = self._get_messages_from_cache(traverse=traverse, to_delete=predicate)
        else:
            deletion_channels = channels
            if channels == "*":
                deletion_channels = [channel for channel in ctx.guild.channels if isinstance(channel, TextChannel)]
            message_mappings, message_ids = await self._get_messages_from_channels(
                traverse=traverse,
                channels=deletion_channels,
                to_delete=predicate,
                until_message=until_message
            )

        if not self.cleaning:
            # Means that the cleaning was canceled
            return

        # Now let's delete the actual messages with purge.
        self.mod_log.ignore(Event.message_delete, *message_ids)
        deleted_messages = await self._delete_found(message_mappings)
        self.cleaning = False

        await self._log_clean(deleted_messages, channels, ctx.author)

        if is_mod_channel(ctx.channel):
            await ctx.message.add_reaction(Emojis.check_mark)

    # region: Commands

    @group(invoke_without_command=True, name="clean", aliases=["clear", "purge"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_group(self, ctx: Context) -> None:
        """Commands for cleaning messages in channels."""
        await ctx.send_help(ctx.command)

    @clean_group.command(name="user", aliases=["users"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_user(
        self,
        ctx: Context,
        user: User,
        traverse: Optional[int] = 10,
        use_cache: Optional[bool] = True,
        *,
        channels: Optional[CleanChannels] = None
    ) -> None:
        """Delete messages posted by the provided user, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(traverse, ctx, user=user, channels=channels, use_cache=use_cache)

    @clean_group.command(name="all", aliases=["everything"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_all(
        self,
        ctx: Context,
        traverse: Optional[int] = 10,
        use_cache: Optional[bool] = True,
        *,
        channels: Optional[CleanChannels] = None
    ) -> None:
        """Delete all messages, regardless of poster, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(traverse, ctx, channels=channels, use_cache=use_cache)

    @clean_group.command(name="bots", aliases=["bot"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_bots(
        self,
        ctx: Context,
        traverse: Optional[int] = 10,
        use_cache: Optional[bool] = True,
        *,
        channels: Optional[CleanChannels] = None
    ) -> None:
        """Delete all messages posted by a bot, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(traverse, ctx, bots_only=True, channels=channels, use_cache=use_cache)

    @clean_group.command(name="regex", aliases=["word", "expression", "pattern"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_regex(
        self,
        ctx: Context,
        regex: str,
        traverse: Optional[int] = 10,
        use_cache: Optional[bool] = True,
        *,
        channels: Optional[CleanChannels] = None
    ) -> None:
        """Delete all messages that match a certain regex, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(traverse, ctx, regex=regex, channels=channels, use_cache=use_cache)

    @clean_group.command(name="until")
    @has_any_role(*MODERATION_ROLES)
    async def clean_until(self, ctx: Context, message: Message) -> None:
        """Delete all messages until certain message, stop cleaning after hitting the `message`."""
        await self._clean_messages(
            CleanMessages.message_limit,
            ctx,
            channels=[message.channel],
            until_message=message
        )

    @clean_group.command(name="between", aliases=["after-until", "from-to"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_between(self, ctx: Context, after_message: Message, until_message: Message) -> None:
        """Delete all messages within range of messages."""
        await self._clean_messages(
            CleanMessages.message_limit,
            ctx,
            channels=[until_message.channel],
            until_message=until_message,
            after_message=after_message,
        )

    @clean_group.command(name="stop", aliases=["cancel", "abort"])
    @has_any_role(*MODERATION_ROLES)
    async def clean_cancel(self, ctx: Context) -> None:
        """If there is an ongoing cleaning process, attempt to immediately cancel it."""
        self.cleaning = False

        embed = Embed(
            color=Colour.blurple(),
            description="Clean interrupted."
        )
        delete_after = 10
        if is_mod_channel(ctx.channel):
            delete_after = None
        await ctx.send(embed=embed, delete_after=delete_after)

    # endregion


def setup(bot: Bot) -> None:
    """Load the Clean cog."""
    bot.add_cog(Clean(bot))
