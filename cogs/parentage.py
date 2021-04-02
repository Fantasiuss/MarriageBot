from datetime import datetime as dt

import asyncpg
import discord
from discord.ext import commands
import voxelbotutils as utils

from cogs import utils as localutils


class Parentage(utils.Cog):
    """
    Parentage, handles parent commands.
    """

    async def get_max_children_for_member(self, guild:discord.Guild, user:discord.Member):
        """
        Get the maximum amount of children a given member can have.
        """

        # See how many children they're allowed with Gold
        gold_children_amount = 0
        if self.bot.config.get('is_server_specific', False):
            guild_max_children = self.bot.guild_settings[guild.id]['max_children']
            if guild_max_children:
                gold_children_amount = max([
                    amount if int(role_id) in user._roles else 0 for role_id, amount in guild_max_children.items()
                ])

        # See how many children they're allowed normally (in regard to Patreon tier)
        marriagebot_perks = await localutils.get_marriagebot_perks(self.bot, user.id)
        normal_children_amount = marriagebot_perks.max_children

        # Return the largest amount of children they've been assigned that's UNDER the global max children as set in the config
        return min([
            max([
                gold_children_amount,
                normal_children_amount,
                min([i['max_children'] for i in self.bot.config['role_perks'].values()]),
            ]),
            max([i['max_children'] for i in self.bot.config['role_perks'].values()]),
        ])

    @utils.command()
    @utils.cooldown.no_raise_cooldown(1, 5, commands.BucketType.user)
    @utils.checks.bot_is_ready()
    @commands.bot_has_permissions(send_messages=True)
    async def makeparent(self, ctx:utils.Context, *, target:localutils.converters.UnblockedMember):
        """
        Picks a user that you want to be your parent.
        """

        # Variables we're gonna need for later
        ctx.author = ctx.author
        instigator_tree, target_tree = localutils.FamilyTreeMember.get_multiple(ctx.author.id, target.id, guild_id=ctx.family_guild_id)

        # Manage output strings
        text_processor = localutils.random_text.RandomText('makeparent', ctx.author, target)
        text = text_processor.process()
        if text:
            return await ctx.send(text)

        # See if our user already has a parent
        if instigator_tree._parent:
            return await ctx.send(text_processor.instigator_is_unqualified())

        # See if they're already related
        async with ctx.channel.typing():
            relation = instigator_tree.get_relation(target_tree)
        if relation and not self.bot.allows_incest(ctx.guild.id):
            return await ctx.send(text_processor.target_is_family())

        # Manage children
        if ctx.original_author_id not in self.bot.owner_ids:
            children_amount = await self.get_max_children_for_member(ctx.guild, target)
            if len(target_tree._children) >= children_amount:
                return await ctx.send("They're currently at the maximum amount of children you can have - see `m!perks` for more information.")

        # Check the size of their trees
        if ctx.original_author_id not in self.bot.owner_ids:
            max_family_members = self.bot.get_max_family_members(ctx.guild)
            async with ctx.channel.typing():
                if instigator_tree.family_member_count + target_tree.family_member_count > max_family_members:
                    return await ctx.send(f"If you added {target.mention} to your family, you'd have over {max_family_members} in your family, so I can't allow you to do that. Sorry!")

        # Valid request - ask the other person
        if not target.bot:
            await ctx.send(text_processor.valid_target())
        await self.bot.proposal_cache.add(ctx.author, target, 'MAKEPARENT')
        check = utils.AcceptanceCheck(target.id, ctx.channel.id)
        try:
            await check.wait_for_response(self.bot)
        except utils.AcceptanceCheck.TIMEOUT:
            return await ctx.send(text_processor.request_timeout(), ignore_error=True)

        # They said no
        if check.response == 'NO':
            await self.bot.proposal_cache.remove()
            return await ctx.send(text_processor.request_denied(), ignore_error=True)

        # They said yes
        async with self.bot.database() as db:
            try:
                await db('INSERT INTO parents (child_id, parent_id, guild_id, timestamp) VALUES ($1, $2, $3, $4)', ctx.author.id, target.id, ctx.family_guild_id, dt.utcnow())
            except asyncpg.UniqueViolationError:
                return await ctx.send("I ran into an error saving your family data - please try again later.")
        await ctx.send(text_processor.request_accepted(), ignore_error=True)

        # Cache
        instigator_tree._parent = target.id
        target_tree._children.append(ctx.author.id)

        # Ping em off over redis
        async with self.bot.redis() as re:
            await re.publish('TreeMemberUpdate', instigator_tree.to_json())
            await re.publish('TreeMemberUpdate', target_tree.to_json())

        # Uncache
        await self.bot.proposal_cache.remove(ctx.author, target)

    @utils.command()
    @utils.cooldown.no_raise_cooldown(1, 5, commands.BucketType.user)
    @utils.checks.bot_is_ready()
    @commands.bot_has_permissions(send_messages=True)
    async def adopt(self, ctx:utils.Context, *, target:localutils.converters.UnblockedMember):
        """
        Adopt another user into your family.
        """

        # Variables we're gonna need for later
        family_guild_id = localutils.get_family_guild_id(ctx)
        author_tree, target_tree = localutils.FamilyTreeMember.get_multiple(ctx.author.id, target.id, guild_id=ctx.family_guild_id)

        # Check they're not a bot
        if target.bot:
            if target.id == self.bot.user.id:
                return await ctx.send("I think I could do better actually, but thank you!")
            return await ctx.send("That is a robot. Robots cannot consent to adoption.")

        # Lock those users
        re = await self.bot.redis.get_connection()
        try:
            lock = await localutils.ProposalLock.lock(re, ctx.author.id, target.id)
        except localutils.ProposalInProgress:
            return await ctx.send("Aren't you popular! One of you is already waiting on a proposal - please try again later.")

        # See if the *target* is already married
        if target_tree.parent:
            await lock.unlock()
            return await ctx.send(
                f"Sorry, {ctx.author.mention}, it looks like {target.mention} already has a parent \N{PENSIVE FACE}",
                allowed_mentions=localutils.only_mention(ctx.author),
            )

        # See if we're already married
        if target.id in author_tree._children:
            await lock.unlock()
            return await ctx.send(
                f"Hey, {ctx.author.mention}, they're already your child \N{FACE WITH ROLLING EYES}",
                allowed_mentions=localutils.only_mention(ctx.author),
            )

        # See if they're already related
        async with ctx.channel.typing():
            relation = author_tree.get_relation(target_tree)
        if relation and localutils.guild_allows_incest(ctx) is False:
            await lock.unlock()
            return await ctx.send(
                f"Woah woah woah, it looks like you guys are already related! You're {target.mention}'s {relation}!",
                allowed_mentions=localutils.only_mention(ctx.author),
            )

        # Manage children
        children_amount = await self.get_max_children_for_member(ctx.guild, ctx.author)
        if len(author_tree._children) >= children_amount:
            return await ctx.send(f"You're currently at the maximum amount of children you can have - see `{ctx.prefix}perks` for more information.")

        # Check the size of their trees
        # TODO I can make this a util because I'm going to use it a couple times
        if ctx.original_author_id not in self.bot.owner_ids:
            max_family_members = localutils.get_max_family_members(ctx)
            async with ctx.channel.typing():
                family_member_count = 0
                for i in author_tree.span(add_parent=True, expand_upwards=True):
                    if family_member_count >= max_family_members:
                        break
                    family_member_count += 1
                for i in target_tree.span(add_parent=True, expand_upwards=True):
                    if family_member_count >= max_family_members:
                        break
                    family_member_count += 1
                if family_member_count >= max_family_members:
                    await lock.unlock()
                    return await ctx.send(
                        f"If you added {target.mention} to your family, you'd have over {max_family_members} in your family. Sorry!",
                        allowed_mentions=localutils.only_mention(ctx.author),
                    )

        # Set up the proposal
        try:
            result = await localutils.send_proposal_message(
                ctx, target,
                f"Hey, {target.mention}, {ctx.author.mention} wants to adopt you! What do you think?",
            )
        except Exception:
            result = None
        if result is None:
            return await lock.unlock()

        # Database it up
        async with self.bot.database() as db:
            try:
                await db(
                    """INSERT INTO parents (parent_id, child_id, guild_id, timestamp) VALUES ($1, $2, $3, $4)""",
                    ctx.author.id, target.id, ctx.family_guild_id, dt.utcnow(),
                )
            except asyncpg.UniqueViolationError:
                await lock.unlock()
                return await ctx.send("I ran into an error saving your family data - please try again later.")
        await ctx.send(f"I'm happy to introduce {ctx.author.mention} as your parent, {target.mention}!", ignore_error=True)

        # And we're done
        author_tree._children.append(target.id)
        target_tree._parent = author_tree.id
        await re.publish('TreeMemberUpdate', author_tree.to_json())
        await re.publish('TreeMemberUpdate', target_tree.to_json())
        await re.disconnect()

    @utils.command(aliases=['abort'])
    @utils.cooldown.no_raise_cooldown(1, 5, commands.BucketType.user)
    @utils.checks.bot_is_ready()
    @commands.bot_has_permissions(send_messages=True)
    async def disown(self, ctx:utils.Context, *, target:utils.converters.UserID):
        """
        Lets you remove a user from being your child.
        """

        # Get the family tree member objects
        family_guild_id = localutils.get_family_guild_id(ctx)
        user_tree, child_tree = localutils.FamilyTreeMember.get(ctx.author.id, target, guild_id=ctx.family_guild_id)
        child_name = await localutils.DiscordNameManager.fetch_name_by_id(self.bot, child_tree.id)

        # Make sure they're actually children
        if child_tree not in user_tree._children:
            return await ctx.send(f"It doesn't look like **{child_name}** is one of your children!", allowed_mentions=discord.AllowedMentions.none())

        # See if they're sure
        try:
            result = await localutils.send_proposal_message(
                ctx, ctx.author,
                f"Are you sure you want to disown your child, {ctx.author.mention}?",
                timeout_message=f"Timed out making sure you want to disown, {ctx.author.mention} :<",
                cancel_message="Alright, I've cancelled your disown!",
            )
        except Exception:
            result = None
        if result is None:
            return

        # Remove from cache
        user_tree._children.remove(child_tree.id)
        child_tree._parent = None

        # Remove from redis
        async with self.bot.redis() as re:
            await re.publish('TreeMemberUpdate', user_tree.to_json())
            await re.publish('TreeMemberUpdate', child_tree.to_json())

        # Remove from database
        async with self.bot.database() as db:
            await db(
                """DELETE FROM parents WHERE child_id=$1 AND parent_id=$2 AND guild_id=$3""",
                child_tree.id, ctx.author.id, family_guild_id,
            )

        # And we're done
        await ctx.send(f"You've successfully disowned **{child_name}** :c", allowed_mentions=discord.AllowedMentions.none())

    @utils.command(aliases=['eman', 'runaway', 'runawayfromhome'])
    @utils.cooldown.no_raise_cooldown(1, 5, commands.BucketType.user)
    @utils.checks.bot_is_ready()
    @commands.bot_has_permissions(send_messages=True)
    async def emancipate(self, ctx:utils.Context):
        """
        Removes your parent.
        """

        # Get the family tree member objects
        family_guild_id = localutils.get_family_guild_id(ctx)
        user_tree = localutils.FamilyTreeMember.get(ctx.author.id, guild_id=ctx.family_guild_id)

        # Make sure they're the child of the instigator
        parent_tree = user_tree.parent
        if not parent_tree:
            return await ctx.send("You don't have a parent right now :<")

        # See if they're sure
        try:
            result = await localutils.send_proposal_message(
                ctx, ctx.author,
                f"Are you sure you want to leave your parent, {ctx.author.mention}?",
                timeout_message=f"Timed out making sure you want to emancipate, {ctx.author.mention} :<",
                cancel_message="Alright, I've cancelled your emancipation!",
            )
        except Exception:
            result = None
        if result is None:
            return

        # Remove family caching
        user_tree._parent = None
        parent_tree._children.remove(ctx.author.id)

        # Ping them off over reids
        async with self.bot.redis() as re:
            await re.publish('TreeMemberUpdate', user_tree.to_json())
            await re.publish('TreeMemberUpdate', parent_tree.to_json())

        # Remove their relationship from the database
        async with self.bot.database() as db:
            await db(
                """DELETE FROM parents WHERE parent_id=$1 AND child_id=$2 AND guild_id=$3""",
                parent_tree.id, ctx.author.id, family_guild_id,
            )

        # And we're done
        parent_name = await localutils.DiscordNameManager.fetch_name_by_id(self.bot, parent_tree.id)
        return await ctx.send(f"You no longer have **{parent_name}** as a parent :c")

    @utils.command()
    @localutils.checks.has_donator_perks("disownall_command")
    @commands.bot_has_permissions(send_messages=True)
    async def disownall(self, ctx:utils.Context):
        """
        Disowns all of your children.
        """

        # Get the family tree member objects
        family_guild_id = localutils.get_family_guild_id(ctx)
        user_tree = localutils.FamilyTreeMember.get(ctx.author.id, guild_id=ctx.family_guild_id)
        child_trees = user_tree.children.copy()
        if not child_trees:
            return await ctx.send("You don't have any children to disown .-.")

        # See if they're sure
        try:
            result = await localutils.send_proposal_message(
                ctx, ctx.author,
                f"Are you sure you want to disown all your children, {ctx.author.mention}?",
                timeout_message=f"Timed out making sure you want to disownall, {ctx.author.mention} :<",
                cancel_message="Alright, I've cancelled your disownall!",
            )
        except Exception:
            result = None
        if result is None:
            return

        # Disown em
        for child in child_trees:
            child._parent = None
        user_tree._children = []

        # Save em
        async with self.bot.database() as db:
            await db(
                """DELETE FROM parents WHERE parent_id=$1 AND guild_id=$2 AND child_id=ANY($3::BIGINT[])""",
                ctx.author.id, family_guild_id, [child.id for child in child_trees],
            )

        # Redis em
        async with self.bot.redis() as re:
            for person in child_trees + [user_tree]:
                await re.publish('TreeMemberUpdate', person.to_json())

        # Output to user
        await ctx.send("You've sucessfully disowned all of your children :c")


def setup(bot:utils.Bot):
    x = Parentage(bot)
    bot.add_cog(x)
