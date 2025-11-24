import random
import datetime
import enum
from datetime import timedelta, timezone

import logging
from typing import TYPE_CHECKING, cast
from ballsdex.core.models import Ball, GuildConfig, Player, balls, Ball as Hero, balls as Heores

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, button
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import RawSQL
from tortoise.functions import Count

from ballsdex.core.models import (
    BallInstance,
    DonationPolicy,
    Player,
    Special,
    Trade,
    TradeObject,
    balls,
)
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.cooldowns import get_cooldown, set_cooldown 
from ballsdex.core.utils.paginator import FieldPageSource, Pages, TextPageSource
from ballsdex.core.utils.sorting import FilteringChoices, SortingChoices, filter_balls, sort_balls
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.core.utils.utils import can_mention, inventory_privacy, is_staff
from ballsdex.packages.balls.countryballs_paginator import CountryballsViewer, DuplicateViewMenu
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.countryballs")
DAILY_COOLDOWN = 24 * 60 * 60  # 24 hours in seconds
WEEKLY_COOLDOWN = 7 * 24 * 60 * 60  # 7 days in seconds
SPECIAL_CHANCE = 0.005

class ModeChoice(enum.StrEnum):
    obtainable = "obtainable"
    unobtainable = "unobtainable"
    all = "all"
class DonationRequest(View):
    def __init__(
        self,
        bot: "BallsDexBot",
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstance,
        new_player: Player,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.original_interaction = interaction
        self.countryball = countryball
        self.new_player = new_player

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
        if interaction.user.id != self.new_player.discord_id:
            await interaction.response.send_message(
                "You are not allowed to interact with this menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # type: ignore
        try:
            await self.original_interaction.followup.edit_message(
                "@original", view=self  # type: ignore
            )
        except discord.NotFound:
            pass
        await self.countryball.unlock()

    @button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore
        self.countryball.favorite = False
        self.countryball.trade_player = self.countryball.player
        self.countryball.player = self.new_player
        await self.countryball.save()
        trade = await Trade.create(player1=self.countryball.trade_player, player2=self.new_player)
        await TradeObject.create(
            trade=trade, ballinstance=self.countryball, player=self.countryball.trade_player
        )
        await interaction.response.edit_message(
            content=interaction.message.content  # type: ignore
            + "\n\N{WHITE HEAVY CHECK MARK} The donation was accepted!",
            view=self,
        )
        await self.countryball.unlock()

    @button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore
        await interaction.response.edit_message(
            content=interaction.message.content  # type: ignore
            + "\n\N{CROSS MARK} The donation was denied.",
            view=self,
        )
        await self.countryball.unlock()


class DuplicateType(enum.StrEnum):
    countryballs = settings.plural_collectible_name
    specials = "specials"


class Balls(commands.GroupCog, group_name=settings.players_group_cog_name):
    """
    View and manage your countryballs collection.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @app_commands.command()
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def list(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        sort: SortingChoices | None = None,
        reverse: bool = False,
        countryball: BallEnabledTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        filter: FilteringChoices | None = None,
    ):
        """
        List your countryballs.

        Parameters
        ----------
        user: discord.User
            The user whose collection you want to view, if not yours.
        sort: SortingChoices
            Choose how countryballs are sorted. Can be used to show duplicates.
        reverse: bool
            Reverse the output of the list.
        countryball: Ball
            Filter the list by a specific countryball.
        special: Special
            Filter the list by a specific special event.
        filter: FilteringChoices
            Filter the list by a specific filter.
        """
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)

        try:
            player = await Player.get(discord_id=user_obj.id)
        except DoesNotExist:
            if user_obj == interaction.user:
                await interaction.followup.send(
                    f"You don't have any {settings.plural_collectible_name} yet."
                )
            else:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any {settings.plural_collectible_name} yet."
                )
            return
        if user is not None:
            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return

        interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        blocked = await player.is_blocked(interaction_player)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot view the list of a user that has you blocked.", ephemeral=True
            )
            return

        query = BallInstance.filter(player=player).prefetch_related("ball", "special")
        if filter:
            query = filter_balls(filter, query, interaction.guild_id)
        if countryball:
            query = query.filter(ball=countryball)
        if special:
            query = query.filter(special=special)
        if sort:
            query = sort_balls(sort, query)
        else:
            query = query.order_by("-favorite")
        countryballs = cast(list[int], await query.values_list("id", flat=True))

        if len(countryballs) < 1:
            ball_txt = countryball.country if countryball else ""
            special_txt = special if special else ""

            if special_txt and ball_txt:
                combined = f"{special_txt} {ball_txt}"
            elif special_txt:
                combined = special_txt
            elif ball_txt:
                combined = ball_txt
            else:
                combined = ""

            if user_obj == interaction.user:
                await interaction.followup.send(
                    f"You don't have any {combined} {settings.plural_collectible_name} yet."
                )
            else:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any {combined} "
                    f"{settings.plural_collectible_name} yet."
                )
            return
        if reverse:
            countryballs.reverse()

        paginator = CountryballsViewer(interaction, countryballs)
        if user_obj == interaction.user:
            await paginator.start()
        else:
            await paginator.start(
                content=f"Viewing {user_obj.name}'s {settings.plural_collectible_name}"
            )

    @app_commands.command()
    @app_commands.checks.cooldown(1, 20, key=lambda i: i.user.id)
    async def completion(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        special: SpecialEnabledTransform | None = None,
        self_caught: bool | None = None,
    ):
        """
        Show your current completion of the BallsDex.

        Parameters
        ----------
        user: discord.User
            The user whose completion you want to view, if not yours.
        special: Special
            The special you want to see the completion of
        self_caught: bool
            Filter only for countryballs that the user themself caught/didn't catch (ie no trades)
        """
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)
        extra_text = f"{special.name} " if special else ""
        if user is not None:
            try:
                player = await Player.get(discord_id=user_obj.id)
            except DoesNotExist:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any "
                    f"{extra_text}{settings.plural_collectible_name} yet."
                )
                return

            interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

            blocked = await player.is_blocked(interaction_player)
            if blocked and not is_staff(interaction):
                await interaction.followup.send(
                    "You cannot view the completion of a user that has blocked you.",
                    ephemeral=True,
                )
                return

            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return
        # Filter disabled balls, they do not count towards progression
        # Only ID and emoji is interesting for us
        bot_countryballs = {x: y.emoji_id for x, y in balls.items() if y.enabled}

        # Set of ball IDs owned by the player
        filters = {"player__discord_id": user_obj.id, "ball__enabled": True}
        if special:
            filters["special"] = special
            bot_countryballs = {
                x: y.emoji_id
                for x, y in balls.items()
                if y.enabled and (special.end_date is None or y.created_at < special.end_date)
            }

        if self_caught is not None:
            filters["trade_player_id__isnull"] = self_caught

        if not bot_countryballs:
            await interaction.followup.send(
                f"There are no {extra_text}{settings.plural_collectible_name}"
                " registered on this bot yet.",
                ephemeral=True,
            )
            return

        owned_countryballs = set(
            x[0]
            for x in await BallInstance.filter(**filters)
            .distinct()  # Do not query everything
            .values_list("ball_id")
        )

        entries: list[tuple[str, str]] = []

        def fill_fields(title: str, emoji_ids: set[int]):
            # check if we need to add "(continued)" to the field name
            first_field_added = False
            buffer = ""

            for emoji_id in emoji_ids:
                emoji = self.bot.get_emoji(emoji_id)
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        entries.append(("\u200b", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    entries.append(("\u200b", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if owned_countryballs:
            # Getting the list of emoji IDs from the IDs of the owned countryballs
            fill_fields(
                f"Owned {settings.plural_collectible_name}",
                set(bot_countryballs[x] for x in owned_countryballs),
            )
        else:
            entries.append((f"__**Owned {settings.plural_collectible_name}**__", "Nothing yet."))

        if missing := set(y for x, y in bot_countryballs.items() if x not in owned_countryballs):
            fill_fields(f"Missing {settings.plural_collectible_name}", missing)
        else:
            entries.append(
                (
                    f"__**:tada: No missing {settings.plural_collectible_name}, "
                    "congratulations! :tada:**__",
                    "\u200b",
                )
            )  # force empty field value

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)
        special_str = f" ({special.name})" if special else ""
        original_catcher_string = (
            f" ({'not ' if self_caught is False else ''}self-caught)"
            if self_caught is not None
            else ""
        )
        source.embed.description = (
            f"{settings.bot_name}{original_catcher_string}{special_str} progression: "
            f"**{round(len(owned_countryballs) / len(bot_countryballs) * 100, 1)}%**"
        )
        source.embed.colour = discord.Colour.blurple()
        source.embed.set_author(name=user_obj.display_name, icon_url=user_obj.display_avatar.url)

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()

    @app_commands.command()
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def info(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Display info from a specific countryball.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to inspect
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        await interaction.response.defer(thinking=True)
        content, file, view = await countryball.prepare_for_message(interaction)
        await interaction.followup.send(content=content, file=file, view=view)
        file.close()

    @app_commands.command()
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def last(
        self, interaction: discord.Interaction["BallsDexBot"], user: discord.User | None = None
    ):
        """
        Display info of your or another users last caught countryball.

        Parameters
        ----------
        user: discord.Member
            The user you would like to see
        """
        user_obj = user if user else interaction.user
        await interaction.response.defer(thinking=True)
        try:
            player = await Player.get(discord_id=user_obj.id)
        except DoesNotExist:
            msg = f"{'You do' if user is None else f'{user_obj.display_name} does'}"
            await interaction.followup.send(
                f"{msg} not have any {settings.plural_collectible_name} yet.",
                ephemeral=True,
            )
            return

        if user is not None:
            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return

        interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        blocked = await player.is_blocked(interaction_player)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                f"You cannot view the last caught {settings.collectible_name} "
                "of a user that has blocked you.",
                ephemeral=True,
            )
            return

        countryball = await player.balls.all().order_by("-id").first().select_related("ball")
        if not countryball:
            msg = f"{'You do' if user is None else f'{user_obj.display_name} does'}"
            await interaction.followup.send(
                f"{msg} not have any {settings.plural_collectible_name} yet.",
                ephemeral=True,
            )
            return

        content, file, view = await countryball.prepare_for_message(interaction)
        if user is not None and user.id != interaction.user.id:
            content = (
                f"You are viewing {user.display_name}'s last caught {settings.collectible_name}.\n"
                + content
            )
        await interaction.followup.send(content=content, file=file, view=view)
        file.close()

    @app_commands.command()
    async def favorite(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Set favorite countryballs.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to set/unset as favorite
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return

        if settings.max_favorites == 0:
            await interaction.response.send_message(
                f"You cannot set favorite {settings.plural_collectible_name} in this bot."
            )
            return

        if not countryball.favorite:
            try:
                player = await Player.get(discord_id=interaction.user.id).prefetch_related("balls")
            except DoesNotExist:
                await interaction.response.send_message(
                    f"You don't have any {settings.plural_collectible_name} yet.", ephemeral=True
                )
                return

            grammar = (
                f"{settings.collectible_name}"
                if settings.max_favorites == 1
                else f"{settings.plural_collectible_name}"
            )
            if await player.balls.filter(favorite=True).count() >= settings.max_favorites:
                await interaction.response.send_message(
                    f"You cannot set more than {settings.max_favorites} favorite {grammar}.",
                    ephemeral=True,
                )
                return

            countryball.favorite = True  # type: ignore
            await countryball.save()
            emoji = self.bot.get_emoji(countryball.countryball.emoji_id) or ""
            await interaction.response.send_message(
                f"{emoji} `#{countryball.pk:0X}` {countryball.countryball.country} "
                f"is now a favorite {settings.collectible_name}!",
                ephemeral=True,
            )

        else:
            countryball.favorite = False  # type: ignore
            await countryball.save()
            emoji = self.bot.get_emoji(countryball.countryball.emoji_id) or ""
            await interaction.response.send_message(
                f"{emoji} `#{countryball.pk:0X}` {countryball.countryball.country} "
                f"isn't a favorite {settings.collectible_name} anymore.",
                ephemeral=True,
            )

    @app_commands.command(extras={"trade": TradeCommandType.PICK})
    async def give(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User,
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Give a countryball to a user.

        Parameters
        ----------
        user: discord.User
            The user you want to give a countryball to
        countryball: BallInstance
            The countryball you're giving away
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        if not countryball.is_tradeable:
            await interaction.response.send_message(
                f"You cannot donate this {settings.collectible_name}.", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message("You cannot donate to bots.", ephemeral=True)
            return
        if await countryball.is_locked():
            await interaction.response.send_message(
                f"This {settings.collectible_name} is currently locked for a trade. "
                "Please try again later.",
                ephemeral=True,
            )
            return
        favorite = countryball.favorite
        if favorite:
            view = ConfirmChoiceView(
                interaction,
                accept_message=f"{settings.collectible_name.title()} donated.",
                cancel_message="This request has been cancelled.",
            )
            await interaction.response.send_message(
                f"This {settings.collectible_name} is a favorite, "
                "are you sure you want to donate it?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.value:
                return
            interaction = view.interaction_response
        else:
            await interaction.response.defer()
        await countryball.lock_for_trade()
        new_player, _ = await Player.get_or_create(discord_id=user.id)
        old_player = countryball.player

        if new_player == old_player:
            await interaction.followup.send(
                f"You cannot give a {settings.collectible_name} to yourself.", ephemeral=True
            )
            await countryball.unlock()
            return
        if new_player.donation_policy == DonationPolicy.ALWAYS_DENY:
            await interaction.followup.send(
                "This player does not accept donations. You can use trades instead.",
                ephemeral=True,
            )
            await countryball.unlock()
            return

        friendship = await new_player.is_friend(old_player)
        if new_player.donation_policy == DonationPolicy.FRIENDS_ONLY:
            if not friendship:
                await interaction.followup.send(
                    "This player only accepts donations from friends, use trades instead.",
                    ephemeral=True,
                )
                await countryball.unlock()
                return
        blocked = await new_player.is_blocked(old_player)
        if blocked:
            await interaction.followup.send(
                "You cannot interact with a user that has blocked you.", ephemeral=True
            )
            await countryball.unlock()
            return
        if new_player.discord_id in self.bot.blacklist:
            await interaction.followup.send(
                "You cannot donate to a blacklisted user.", ephemeral=True
            )
            await countryball.unlock()
            return
        elif new_player.donation_policy == DonationPolicy.REQUEST_APPROVAL:
            await interaction.followup.send(
                f"Hey {user.mention}, {interaction.user.name} wants to give you "
                f"{countryball.description(include_emoji=True, bot=self.bot, is_trade=True)}!\n"
                "Do you accept this donation?",
                view=DonationRequest(self.bot, interaction, countryball, new_player),
                allowed_mentions=await can_mention([new_player, old_player]),
            )
            return

        countryball.player = new_player
        countryball.trade_player = old_player
        countryball.favorite = False
        await countryball.save()

        trade = await Trade.create(player1=old_player, player2=new_player)
        await TradeObject.create(trade=trade, ballinstance=countryball, player=old_player)

        cb_txt = (
            countryball.description(short=True, include_emoji=True, bot=self.bot, is_trade=True)
            + f" (`{countryball.attack_bonus:+}%/{countryball.health_bonus:+}%`)"
        )
        if favorite:
            await interaction.followup.send(
                f"{interaction.user.mention}, you just gave the "
                f"{settings.collectible_name} {cb_txt} to {user.mention}!",
                allowed_mentions=await can_mention([new_player, old_player]),
            )
        else:
            await interaction.followup.send(
                f"You just gave the {settings.collectible_name} {cb_txt} to {user.mention}!",
                allowed_mentions=await can_mention([new_player]),
            )
        await countryball.unlock()

    @app_commands.command()
    async def count(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        current_server: bool = False,
    ):
        """
        Count how many countryballs you have.

        Parameters
        ----------
        countryball: Ball
            The countryball you want to count
        special: Special
            The special you want to count
        current_server: bool
            Only count countryballs caught in the current server
        """
        if interaction.response.is_done():
            return

        assert interaction.guild
        filters = {}
        if countryball:
            filters["ball"] = countryball
        if special:
            filters["special"] = special
        if current_server:
            filters["server_id"] = interaction.guild.id
        filters["player__discord_id"] = interaction.user.id

        await interaction.response.defer(ephemeral=True, thinking=True)

        balls = await BallInstance.filter(**filters).count()
        country = f"{countryball.country} " if countryball else ""
        plural = "s" if balls > 1 or balls == 0 else ""
        special_str = f"{special.name} " if special else ""
        guild = f" caught in {interaction.guild.name}" if current_server else ""

        await interaction.followup.send(
            f"You have {balls:,} {special_str}"
            f"{country}{settings.collectible_name}{plural}{guild}."
        )

    @app_commands.command()
    @app_commands.checks.cooldown(1, 20, key=lambda i: i.user.id)
    async def duplicate(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        type: DuplicateType,
        limit: int | None = None,
    ):
        """
        Shows your most duplicated countryballs or specials.

        Parameters
        ----------
        type: DuplicateType
            Type of duplicate to check (countryballs or specials).
        limit: int | None
            The amount of countryballs to show, can only be used with `countryballs`.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)

        player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        is_special = type == DuplicateType.specials
        queryset = BallInstance.filter(player=player)

        if is_special:
            queryset = queryset.filter(special_id__isnull=False).prefetch_related("special")
            annotations = {"name": "special__name", "emoji": "special__emoji"}
            apply_limit = False
        else:
            queryset = queryset.filter(ball__tradeable=True)
            annotations = {"name": "ball__country", "emoji": "ball__emoji_id"}
            apply_limit = True

        query = (
            queryset.annotate(count=Count("id")).group_by(*annotations.values()).order_by("-count")
        )

        if apply_limit and limit is not None:
            query = query.limit(limit)

        query = query.values(*annotations.values(), "count")
        results = await query

        if not results:
            await interaction.followup.send(
                f"You don't have any {type.value} duplicates in your inventory.", ephemeral=True
            )
            return

        entries = [
            {
                "name": item[annotations["name"]],
                "emoji": (
                    self.bot.get_emoji(item[annotations["emoji"]]) or item[annotations["emoji"]]
                ),
                "count": item["count"],
            }
            for item in results
        ]

        source = DuplicateViewMenu(interaction, entries, type.value)
        await source.start(content=f"View your duplicate {type.value}.")

    @app_commands.command()
    @app_commands.checks.cooldown(1, 20, key=lambda i: i.user.id)
    async def compare(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Compare your countryballs with another user.

        Parameters
        ----------
        user: discord.User
            The user you want to compare with
        special: Special
            Filter the results of the comparison to a special event.
        """
        await interaction.response.defer(thinking=True)
        if interaction.user == user:
            await interaction.followup.send("You cannot compare with yourself.", ephemeral=True)
            return

        try:
            player = await Player.get(discord_id=user.id)
        except DoesNotExist:
            await interaction.followup.send(
                f"{user.display_name} doesn't have any {settings.plural_collectible_name} yet."
            )
            return

        if await inventory_privacy(self.bot, interaction, player, user) is False:
            return

        bot_countryballs = {x: y.emoji_id for x, y in balls.items() if y.enabled}
        if special:
            bot_countryballs = {
                x: y.emoji_id
                for x, y in balls.items()
                if y.enabled and (special.end_date is None or y.created_at < special.end_date)
            }

        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)

        blocked = await player.is_blocked(player1)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot compare with a user that has you blocked.", ephemeral=True
            )
            return

        blocked = await player.is_blocked(player2)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot compare with a user that has you blocked.", ephemeral=True
            )
            return
        queryset = BallInstance.filter(ball__enabled=True).distinct()
        if special:
            queryset = queryset.filter(special=special)
        user1_balls = cast(
            list[int],
            await queryset.filter(player=player1).values_list("ball_id", flat=True),
        )
        user2_balls = cast(
            list[int],
            await queryset.filter(player=player2).values_list("ball_id", flat=True),
        )
        both = set(user1_balls) & set(user2_balls)
        user1_only = set(user1_balls) - set(user2_balls)
        user2_only = set(user2_balls) - set(user1_balls)
        neither = set(bot_countryballs.keys()) - both - user1_only - user2_only

        entries = []

        def fill_fields(title: str, ids: set[int]):
            first_field_added = False
            buffer = ""

            for ball_id in ids:
                emoji = self.bot.get_emoji(bot_countryballs[ball_id])
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        entries.append(("\u200b", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    entries.append(("\u200b", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if both:
            fill_fields("Both have", both)
        else:
            entries.append(("__**Both have**__", "None"))
        fill_fields(f"{interaction.user.display_name} has", user1_only)
        fill_fields(f"{user.display_name} has", user2_only)
        fill_fields("Neither have", neither)

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)
        special_str = f" ({special.name})" if special else ""
        source.embed.title = (
            f"Comparison of {interaction.user.display_name} and {user.display_name}'s "
            f"{settings.plural_collectible_name}{special_str}"
        )
        source.embed.colour = discord.Colour.blurple()

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()

    @app_commands.command()
    async def collection(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        ephemeral: bool = False,
    ):
        """
        Show the collection of a specific countryball.

        Parameters
        ----------
        countryball: Ball
            The countryball you want to see the collection of
        ephemeral: bool
            Whether or not to send the command ephemerally.
        """
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        query = (
            BallInstance.filter(player=player)
            .annotate(
                total=RawSQL("COUNT(*)"),
                traded=RawSQL("SUM(CASE WHEN trade_player_id IS NULL THEN 0 ELSE 1 END)"),
                specials=RawSQL("SUM(CASE WHEN special_id IS NULL THEN 0 ELSE 1 END)"),
            )
            .group_by("player_id")
        )
        specials = (
            BallInstance.filter(player=player)
            .exclude(special=None)
            .annotate(count=Count("id"))
            .group_by("special__name")
        )
        if countryball:
            query = query.filter(ball=countryball)
            specials = specials.filter(ball=countryball)
        counts_list = await query.values("player_id", "total", "traded", "specials")
        specials = await specials.values("special__name", "count")

        if not counts_list:
            if countryball:
                await interaction.followup.send(
                    f"You don't have any {countryball.country} "
                    f"{settings.plural_collectible_name} yet."
                )
            else:

                await interaction.followup.send(
                    f"You don't have any {settings.plural_collectible_name} yet."
                )
            return
        counts = counts_list[0]
        all_specials = await Special.filter(hidden=False)
        special_emojis = {x.name: x.emoji for x in all_specials}

        desc = (
            f"**Total**: {counts["total"]:,} ({counts["total"] - counts["traded"]:,} caught, "
            f"{counts['traded']:,} received from trade)\n"
            f"**Total Specials**: {counts['specials']:,}\n\n"
        )
        if counts["specials"]:
            desc += "**Specials**:\n"
        for special in sorted(specials, key=lambda x: x["count"], reverse=True):
            emoji = special_emojis.get(special["special__name"], "")
            desc += f"{emoji} {special['special__name']}: {special["count"]:,}\n"

        embed = discord.Embed(
            title=f"Collection of {countryball.country}" if countryball else "Total Collection",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )
        if countryball:
            emoji = self.bot.get_emoji(countryball.emoji_id)
            if emoji:
                embed.set_thumbnail(url=emoji.url)
        await interaction.followup.send(embed=embed)
        
    @app_commands.command(name="all")
    async def md_all(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        mode: ModeChoice = ModeChoice.obtainable,
    ):
        """
        List all heroes in the bot, sorted by rarity (rarest first).

        Parameters
        ----------
        mode: ModeChoice
            Whether to list obtainable, unobtainable, or all heroes.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)

        # Order by -rarity (descending) for rarest first
        hero_queryset = Hero.all().order_by("-rarity") 

        if mode == ModeChoice.obtainable:
            hero_queryset = hero_queryset.filter(enabled=True)
        elif mode == ModeChoice.unobtainable:
            hero_queryset = hero_queryset.filter(enabled=False)

        # Fetch country (the name field) and rarity
        heroes = await hero_queryset.values_list("country", "rarity")

        if not heroes:
            await interaction.followup.send("No heroes found matching that criteria.", ephemeral=True)
            return

        # Prepare list for FieldPageSource: (name, value) tuples.
        # We will use the hero name as the field name and its rarity as the value.
        field_list = [
            (name, f"Rarity: {rarity}") 
            for name, rarity in heroes
        ]
        
        # Use FieldPageSource for proper embed pagination (25 items per page)
        source = FieldPageSource(
            field_list,
            per_page=25,
        )
        
        # Set the title and color directly on the source, which FieldPageSource supports.
        source.embed.title = f"Heroes - {mode.name.title()}"
        source.embed.color = discord.Color.blue()
        
        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start(ephemeral=True)
        
    async def _sample_hero(self, obtainable_only: bool = True, rare_only: bool = False):
        """
        Best-effort sampling:
        - Prefer Ball.spawn_weight if present.
        - Fallback to inverse of ball.rarity (1/rarity) if rarity > 0.
        - Final fallback: uniform weights.
        """
        qs = Hero.all()
        if obtainable_only:
            qs = qs.filter(enabled=True)
        if rare_only:
            # user said "rare means subs 1.0" -> filter rarity == 1.0 (adjust if your schema differs)
            qs = qs.filter(rarity=1.0)

        heroes = await qs  # returns list of model instances

        if not heroes:
            return None

        weights = []
        for h in heroes:
            # If your Ball model actually has 'spawn_weight' or similar, it will be preferred:
            w = getattr(h, "spawn_weight", None)
            if w is None:
                # fallback to inverse of rarity if available
                rarity = getattr(h, "rarity", None)
                if rarity is None:
                    w = 1.0
                else:
                    # avoid division by zero; a higher rarity value -> rarer item,
                    # so inverse gives more weight to common items.
                    w = 1.0 / (rarity if rarity > 0 else 1.0)
            weights.append(w)

        # Normalize or pass directly to random.choices
        try:
            chosen = random.choices(heroes, weights=weights, k=1)[0]
        except Exception:
            # final fallback: uniform random
            chosen = random.choice(heroes)
        return chosen


    @app_commands.command(name="daily")
    @app_commands.checks.cooldown(1, DAILY_COOLDOWN, key=lambda i: i.user.id)
    async def md_daily(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        /md daily - give the user a single random hero. 24h cooldown.
        Odds use the same weighting logic as natural spawns (best-effort).
        Small chance to be a special instead.
        """
        await interaction.response.defer(thinking=True)

        # get or create player
        player, _ = await Player.filter(discord_id=interaction.user.id).only("discord_id", "id").get_or_create()

        # Decide if reward will be a special instead
        give_special = random.random() < SPECIAL_CHANCE

        # Sample a hero (obtainable only)
        hero = await self._sample_hero(obtainable_only=True, rare_only=False)
        if not hero:
            await interaction.followup.send(
                "No heroes are registered on this bot right now.", ephemeral=True
            )
            return

        # Check ownership before creating the instance:
        owned_any = await BallInstance.filter(player=player, ball=hero).exists()

        # Create stats (attack and health bonuses are percent-style ints)
        attack_bonus = random.randint(-20, 20)
        health_bonus = random.randint(-20, 20)

        # Create the BallInstance. Adapt fields to your actual schema if different.
        try:
            instance = await BallInstance.create(
                ball=hero,
                player=player,
                attack_bonus=attack_bonus,
                health_bonus=health_bonus,
                favorite=False,
            )
        except Exception:
            log.exception("Failed to create BallInstance for /md daily")
            await interaction.followup.send(
                "Something went wrong creating your packed hero. Contact an admin.",
                ephemeral=True,
            )
            return

        # Optionally attach a special (best-effort)
        special_text = ""
        if give_special:
            try:
                specials = await Special.filter(hidden=False).all()
                if specials:
                    chosen_special = random.choice(specials)
                    instance.special = chosen_special
                    await instance.save()
                    special_text = f" (Special: {chosen_special.name})"
            except Exception:
                log.exception("Failed to attach special for /md daily")

        # Message formatting:
        sign = "+" if attack_bonus >= 0 else ""
        sign2 = "+" if health_bonus >= 0 else ""
        instance_id_hex = f"{instance.pk:X}" if getattr(instance, "pk", None) is not None else str(
            instance.id if getattr(instance, "id", None) is not None else "0"
        )
        first_line = (
            f"{interaction.user.mention} You packed **{hero.country}**! "
            f"(#{instance_id_hex}, {sign2}{health_bonus}%/{sign}{attack_bonus}%)"
            f"{special_text}"
        )

        completion_msg = ""
        if not owned_any:
            completion_msg = "\n\nThis is a new footdex that has been added to your completion!"

        await interaction.followup.send(first_line + completion_msg)


    @app_commands.command(name="weekly")
    @app_commands.checks.cooldown(1, WEEKLY_COOLDOWN, key=lambda i: i.user.id)
    async def md_weekly(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        /md weekly - give the user a random *rare* hero (rarity == 1.0 per your note).
        7 day cooldown.
        """
        await interaction.response.defer(thinking=True)

        player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        give_special = random.random() < SPECIAL_CHANCE

        hero = await self._sample_hero(obtainable_only=True, rare_only=True)
        if not hero:
            hero = await self._sample_hero(obtainable_only=True, rare_only=False)
            if not hero:
                await interaction.followup.send(
                    "No heroes are registered on this bot right now.", ephemeral=True
                )
                return

        owned_any = await BallInstance.filter(player=player, ball=hero).exists()

        attack_bonus = random.randint(-20, 20)
        health_bonus = random.randint(-20, 20)

        try:
            instance = await BallInstance.create(
                ball=hero,
                player=player,
                attack_bonus=attack_bonus,
                health_bonus=health_bonus,
                favorite=False,
            )
        except Exception:
            log.exception("Failed to create BallInstance for /md weekly")
            await interaction.followup.send(
                "Something went wrong creating your packed hero. Contact an admin.",
                ephemeral=True,
            )
            return

        special_text = ""
        if give_special:
            try:
                specials = await Special.filter(hidden=False).all()
                if specials:
                    chosen_special = random.choice(specials)
                    instance.special = chosen_special
                    await instance.save()
                    special_text = f" (Special: {chosen_special.name})"
            except Exception:
                log.exception("Failed to attach special for /md weekly")

        sign = "+" if attack_bonus >= 0 else ""
        sign2 = "+" if health_bonus >= 0 else ""
        instance_id_hex = f"{instance.pk:X}" if getattr(instance, "pk", None) is not None else str(
            instance.id if getattr(instance, "id", None) is not None else "0"
        )

        first_line = (
            f"{interaction.user.mention} You packed **{hero.country}**! "
            f"(#{instance_id_hex}, {sign2}{health_bonus}%/{sign}{attack_bonus}%)"
            f"{special_text}"
        )

        completion_msg = ""
        if not owned_any:
            completion_msg = "\n\nThis is a new footdex that has been added to your completion!"

        await interaction.followup.send(first_line + completion_msg)