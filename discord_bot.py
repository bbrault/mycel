from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from arcane import Arcane
from message_bus import Message

load_dotenv()

logger = logging.getLogger("arcane.discord")

DISCORD_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
DEFAULT_CHANNEL = "agents"
CHUNK_SIZE = 1900


def chunk_message(text: str) -> list[str]:
    """Split a message into chunks that fit Discord's 2000-char limit."""
    if len(text) <= CHUNK_SIZE:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= CHUNK_SIZE:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, CHUNK_SIZE)
        if split_at == -1:
            split_at = CHUNK_SIZE
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


Sendable = Union[discord.TextChannel, discord.Thread]


class CircleControlView(discord.ui.View):
    """Interactive buttons for forge pause actions (Resume / Retry / Reset)."""

    def __init__(self, arcane: Arcane, forge_name: str, show_push: bool = False) -> None:
        super().__init__(timeout=3600)  # 1h timeout
        self.arcane = arcane
        self.forge_name = forge_name
        if show_push:
            push_btn = discord.ui.Button(
                label="Push + MR",
                style=discord.ButtonStyle.green,
                emoji="\U0001f680",
                custom_id=f"push_{forge_name}",
            )
            push_btn.callback = self._push_callback
            self.add_item(push_btn)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.blurple, emoji="\u25b6\ufe0f")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        self.arcane.resume_circle(self.forge_name)
        await interaction.response.send_message(
            f"\u25b6\ufe0f **Circle {self.forge_name}** \u2192 reprise du workflow",
        )
        self.stop()

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.blurple, emoji="\U0001f504")
    async def retry_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        self.arcane.retry_circle(self.forge_name)
        await interaction.response.send_message(
            f"\U0001f504 **Circle {self.forge_name}** \u2192 relance du skill actuel",
        )
        self.stop()

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.red, emoji="\U0001f5d1\ufe0f")
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        self.arcane.reset_circle(self.forge_name)
        await interaction.response.send_message(
            f"\U0001f5d1\ufe0f **Circle {self.forge_name}** \u2192 reinitialisee",
        )
        self.stop()

    async def _push_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        forge = self.arcane.circles.get(self.forge_name)
        if not forge:
            await interaction.followup.send("\u274c Forge introuvable")
            return
        try:
            await forge._finalize_git(forge.state.get("current_skill", "implement"))
            await interaction.followup.send(
                f"\U0001f680 **Circle {self.forge_name}** \u2192 Push + creation MR termines. Voir les messages ci-dessus pour les details.",
            )
        except Exception as exc:
            await interaction.followup.send(f"\u274c Push echoue : {exc}")


class ArcaneBot(commands.Bot):
    """Discord bot that drives the Arcane system."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.arcane = Arcane()
        # Multi-channel support: channel_name -> TextChannel
        self._channels: Dict[str, discord.TextChannel] = {}
        # Active thread per forge run: forge_name -> Thread
        self._forge_threads: Dict[str, discord.Thread] = {}
        # Streaming messages: streaming_id -> discord.Message (for edit instead of flood)
        self._streaming_messages: Dict[str, discord.Message] = {}
        # Pinned dashboard message (auto-updated)
        self._dashboard_msg: Optional[discord.Message] = None

    @property
    def agents_channel(self) -> Optional[discord.TextChannel]:
        """Default channel for welcome messages and crew-level commands."""
        return self._channels.get(DEFAULT_CHANNEL)

    async def setup_hook(self) -> None:
        self.arcane.bus.subscribe(self._on_bus_message)
        await self.arcane.bus.start()
        await self.arcane.start_worker()
        await self.arcane.start_sentry_monitor()
        # Register slash commands
        _register_slash_commands(self)
        try:
            await self.tree.sync()
            logger.info("Slash commands synchronisees")
        except Exception as exc:
            logger.warning("Impossible de synchroniser les slash commands: %s", exc)

    async def on_ready(self) -> None:
        logger.info("Connecte en tant que %s", self.user)

        # Collect all channel names from forge configs + default
        channel_names = {DEFAULT_CHANNEL}
        for forge_cfg in self.arcane.config.get("forges", {}).values():
            ch = forge_cfg.get("channel")
            if ch:
                channel_names.add(ch)

        # Discover channels across all guilds
        for guild in self.guilds:
            for channel in guild.text_channels:
                if channel.name in channel_names:
                    self._channels[channel.name] = channel

        for name in channel_names:
            if name in self._channels:
                logger.info("Canal #%s trouve", name)
            else:
                logger.warning("Canal #%s introuvable — creez-le dans Discord", name)

        if self.agents_channel:
            await self._send_welcome()
            await self._update_dashboard()

    # ------------------------------------------------------------------
    # Channel & thread routing
    # ------------------------------------------------------------------

    def _get_forge_channel_name(self, forge_name: str) -> str:
        """Return the Discord channel name configured for a forge."""
        forge_cfg = self.arcane.config.get("forges", {}).get(forge_name, {})
        return forge_cfg.get("channel", DEFAULT_CHANNEL)

    def _get_forge_channel(self, forge_name: str) -> Optional[discord.TextChannel]:
        """Return the Discord channel for a forge, falling back to agents."""
        channel_name = self._get_forge_channel_name(forge_name)
        return self._channels.get(channel_name) or self.agents_channel

    async def _create_forge_thread(
        self,
        forge_name: str,
        task: str,
    ) -> Optional[discord.Thread]:
        """Get or create a thread in the forge's channel for this workflow run.

        Reuses an existing active (non-archived) thread for this forge
        instead of creating duplicates.
        """
        # Reuse existing thread if still active
        existing = self._forge_threads.get(forge_name)
        if existing is not None:
            try:
                # Check if thread is still alive and not archived
                thread = existing.guild.get_thread(existing.id)  # type: ignore[union-attr]
                if thread and not thread.archived:
                    logger.info("Thread existant reutilise pour forge %s: %s", forge_name, thread.name)
                    return thread
            except Exception:
                pass
            # Thread gone or archived — remove reference
            self._forge_threads.pop(forge_name, None)

        channel = self._get_forge_channel(forge_name)
        if channel is None:
            return None

        # Build thread name (Discord limit: 100 chars)
        thread_name = f"\U0001f527 {forge_name} \u2014 {task[:80]}"
        if len(thread_name) > 100:
            thread_name = thread_name[:97] + "..."

        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,  # 24h
            )
            self._forge_threads[forge_name] = thread
            logger.info("Thread cree pour forge %s: %s", forge_name, thread_name)
            return thread
        except Exception as exc:
            logger.error("Erreur creation thread pour forge %s: %s", forge_name, exc)
            return None

    def _get_send_target(self, forge_name: str = "") -> Optional[Sendable]:
        """Return the best target for a forge message: thread > channel > agents."""
        if forge_name and forge_name in self._forge_threads:
            return self._forge_threads[forge_name]
        if forge_name:
            return self._get_forge_channel(forge_name)
        return self.agents_channel

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------

    async def _send_to_target(self, text: str, target: Sendable) -> None:
        """Send chunked text to a channel or thread."""
        for chunk in chunk_message(text):
            if chunk.strip():
                try:
                    await target.send(chunk)
                except Exception as exc:
                    logger.error("Erreur envoi Discord: %s", exc)

    async def _send_to_channel(self, text: str) -> None:
        """Send to the default agents channel (welcome, crew commands)."""
        if self.agents_channel is None:
            return
        await self._send_to_target(text, self.agents_channel)

    # ------------------------------------------------------------------
    # Bus message handler
    # ------------------------------------------------------------------

    async def _on_bus_message(self, message: Message) -> None:
        target = self._get_send_target(message.forge_name)
        if target is None:
            logger.warning("Bus message ignore (pas de cible): %s", message.content[:80])
            return
        logger.debug("Discord <- [%s] %s", message.forge_name, message.content[:80])

        # Update pinned dashboard on non-debug messages
        if message.level != "debug":
            try:
                await self._update_dashboard()
            except Exception:
                pass

        # Streaming update: edit existing message instead of sending a new one
        stream_id = message.data.get("streaming_id")
        if message.data.get("streaming_update") and stream_id:
            existing = self._streaming_messages.get(stream_id)
            try:
                if existing:
                    await existing.edit(content=message.content[:1900])
                else:
                    sent = await target.send(message.content[:1900])
                    self._streaming_messages[stream_id] = sent
            except Exception as exc:
                logger.debug("Streaming edit echoue: %s", exc)
            return

        # Clean up streaming message when skill finishes (non-streaming message arrives)
        if message.forge_name:
            for sid in list(self._streaming_messages):
                if sid.startswith(f"{message.forge_name}_"):
                    self._streaming_messages.pop(sid, None)

        # If this is a pause message, send with interactive buttons
        if message.data.get("paused") and message.forge_name:
            skill_name = message.data.get("skill_name", "")
            skill_cfg = self.arcane.spells_config.get(skill_name, {})
            show_push = skill_cfg.get("git_prepare", False)
            view = CircleControlView(self.arcane, message.forge_name, show_push=show_push)
            try:
                await target.send(message.content, view=view)
            except Exception as exc:
                logger.error("Erreur envoi boutons Discord: %s", exc)
                await self._send_to_target(message.content, target)
        else:
            await self._send_to_target(message.content, target)

        # Upload output file as attachment if present
        output_file = message.data.get("output_file")
        if output_file and os.path.isfile(output_file):
            try:
                file = discord.File(output_file, filename=os.path.basename(output_file))
                await target.send(
                    f"\U0001f4ce Fichier : `{os.path.basename(output_file)}`",
                    file=file,
                )
            except Exception as exc:
                logger.error("Erreur upload fichier Discord: %s", exc)

    # ------------------------------------------------------------------
    # Welcome message
    # ------------------------------------------------------------------

    async def _update_dashboard(self) -> None:
        """Update (or create) the pinned dashboard message in #agents."""
        if self.agents_channel is None:
            return

        # Build compact dashboard with progress bars
        lines = ["\U0001f9e0 **Arcane Grimoire**\n"]
        for name, forge in self.arcane.circles.items():
            lines.append(forge.progress_bar)

        queue_total = self.arcane.queue_size
        if queue_total > 0:
            lines.append(f"\n\U0001f4cb File d'attente : {queue_total} tache(s)")

        content = "\n".join(lines)

        try:
            if self._dashboard_msg:
                await self._dashboard_msg.edit(content=content)
            else:
                self._dashboard_msg = await self.agents_channel.send(content)
                try:
                    await self._dashboard_msg.pin()
                except Exception:
                    pass  # Pin might fail if no permission
        except discord.NotFound:
            # Message was deleted, recreate
            self._dashboard_msg = await self.agents_channel.send(content)
        except Exception as exc:
            logger.debug("Dashboard update echoue: %s", exc)

    async def _send_welcome(self) -> None:
        forges_list = "\n".join(
            f"\u2022 `!{name}` \u2014 {forge.description} (#{self._get_forge_channel_name(name)})"
            for name, forge in self.arcane.circles.items()
        )
        check = "\u2705"
        cross = "\u274c"
        runners = ", ".join(
            f"{k}: {check if v else cross}"
            for k, v in self.arcane.familiar_status.items()
        )

        channels_str = ", ".join(f"#{name}" for name in sorted(self._channels.keys()))

        welcome = (
            "\U0001f9e0 **Arcane en ligne**\n\n"
            f"**Runners :** {runners}\n"
            f"**Canaux :** {channels_str}\n\n"
            f"**Forges disponibles :**\n{forges_list}\n\n"
            "**Commandes :**\n"
            "\u2022 `!<forge> <description>` \u2192 lancer le workflow (cree un thread)\n"
            "\u2022 `!<forge> skill <skill> [instructions]` \u2192 executer un skill isole\n"
            "\u2022 `!<forge> from <skill>` \u2192 reprendre depuis une etape\n"
            "\u2022 `!<forge> resume [instructions]` \u2192 reprendre apres une pause\n"
            "\u2022 `!<forge> retry [instructions]` \u2192 relancer le skill actuel\n"
            "\u2022 `!<forge> status` \u2192 progression du workflow\n"
            "\u2022 `!<forge> log [N]` \u2192 derniers N messages\n"
            "\u2022 `!<forge> reset` \u2192 reset une forge\n"
            "\u2022 `!crew status` \u2192 etat global\n"
            "\u2022 `!crew forges` \u2192 liste des forges\n"
            "\u2022 `!crew skills` \u2192 liste des skills\n"
            "\u2022 `!crew sentry [check|start|stop|status]` \u2192 monitoring Sentry\n"
            "\u2022 `!crew reset` \u2192 reset toutes les forges\n\n"
            "Envoyez un message dans un thread actif pour injecter du feedback."
        )
        await self._send_to_channel(welcome)

    # ------------------------------------------------------------------
    # Dynamic forge command handler
    # ------------------------------------------------------------------

    def _check_permission(self, member: discord.Member, forge_name: str) -> bool:
        """Check if a member has permission to use a forge based on role config."""
        permissions = self.arcane.config.get("permissions", {})
        if not permissions:
            return True  # No permissions configured -> allow all

        member_roles = {r.name for r in member.roles}
        matched = False
        for role_name, allowed in permissions.items():
            if role_name == "default":
                continue  # Check default last
            if role_name not in member_roles:
                continue
            matched = True
            if allowed == "all":
                return True
            if isinstance(allowed, list) and forge_name in allowed:
                return True

        # No role matched -> check default policy
        if not matched:
            default = permissions.get("default", "all")
            if default == "all":
                return True
            if isinstance(default, list) and forge_name in default:
                return True

        return False

    async def _handle_forge_command(
        self,
        message: discord.Message,
        forge_name: str,
        rest: str,
    ) -> None:
        """Handle a dynamic forge command: !<forge_name> <action|task>."""
        channel = message.channel

        # Permission check (skip for status/log — read-only)
        action_lower = rest.split()[0].lower() if rest.strip() else ""
        if action_lower not in ("status", "log") and isinstance(message.author, discord.Member):
            if not self._check_permission(message.author, forge_name):
                await channel.send(f"\U0001f6ab Vous n'avez pas la permission d'utiliser la forge **{forge_name}**.")
                return

        parts = rest.split(maxsplit=1) if rest else []
        action = parts[0] if parts else ""
        action_lower = action.lower()
        args = parts[1] if len(parts) > 1 else ""

        if action_lower == "status":
            status = self.arcane.get_circle_status(forge_name)
            target = self._get_send_target(forge_name)
            if target:
                await self._send_to_target(status, target)
            else:
                await channel.send(status)

        elif action_lower == "reset":
            self.arcane.reset_circle(forge_name)
            self._forge_threads.pop(forge_name, None)
            await channel.send(f"\U0001f527 **Circle {forge_name}** \u2192 reinitialisee.")

        elif action_lower == "log":
            limit = 20
            if args.strip().isdigit():
                limit = int(args.strip())
            log_text = self.arcane.get_circle_log(forge_name, limit=limit)
            target = self._get_send_target(forge_name)
            if target:
                await self._send_to_target(log_text, target)
            else:
                await channel.send(log_text)

        elif action_lower == "from":
            if not args:
                await channel.send(f"Usage : `!{forge_name} from <skill> [instructions]`")
                return
            from_parts = args.split(maxsplit=1)
            from_skill = from_parts[0]
            from_instructions = from_parts[1] if len(from_parts) > 1 else None

            forge = self.arcane.circles[forge_name]
            if from_skill not in forge.workflow:
                available_skills = " \u2192 ".join(forge.workflow)
                await channel.send(f"Skill `/{from_skill}` pas dans le workflow. Disponibles : {available_skills}")
                return

            task = forge.state.get("task") or "reprise"
            thread = await self._create_forge_thread(forge_name, task)

            idx = forge.workflow.index(from_skill)
            kept = forge.workflow[:idx]
            rerun = forge.workflow[idx:]
            kept_str = ", ".join(f"\u2705 `/{s}`" for s in kept) if kept else "(rien)"
            rerun_str = " \u2192 ".join(f"`/{s}`" for s in rerun)

            await self.arcane.enqueue_from_spell(forge_name, from_skill, from_instructions)

            msg = (
                f"\U0001f504 **Circle {forge_name}** \u2192 reprise depuis `/{from_skill}`\n"
                f"\U0001f4e6 Conserve : {kept_str}\n"
                f"\U0001f501 Relance : {rerun_str}"
            )
            await channel.send(msg)
            if thread:
                await self._send_to_target(msg, thread)

        elif action_lower == "resume":
            if self.arcane.circles[forge_name].state["status"] != "paused":
                await channel.send(f"\U0001f527 **Circle {forge_name}** n'est pas en pause.")
                return
            self.arcane.resume_circle(forge_name, instructions=args or None)
            await channel.send(f"\u25b6\ufe0f **Circle {forge_name}** \u2192 reprise du workflow")

        elif action_lower == "abort":
            aborted = self.arcane.abort_circle(forge_name)
            if aborted:
                await channel.send(f"\U0001f6d1 **Circle {forge_name}** \u2192 skill en cours avorte. Utilisez `!{forge_name} from <skill>` pour reprendre.")
            else:
                await channel.send(f"\U0001f527 **Circle {forge_name}** n'est pas en cours d'execution.")

        elif action_lower == "retry":
            if self.arcane.circles[forge_name].state["status"] not in ("paused", "failed", "error"):
                await channel.send(f"\U0001f527 **Circle {forge_name}** n'est pas en pause ou en erreur.")
                return
            self.arcane.retry_circle(forge_name, instructions=args or None)
            await channel.send(f"\U0001f504 **Circle {forge_name}** \u2192 relance du skill actuel")

        elif action_lower == "skill":
            skill_parts = args.split(maxsplit=1) if args else []
            if not skill_parts:
                await channel.send(f"Usage : `!{forge_name} skill <skill_name> [instructions]`")
                return
            skill_name = skill_parts[0]
            skill_instructions = skill_parts[1] if len(skill_parts) > 1 else None

            if skill_name not in self.arcane.spells_config:
                available_skills = ", ".join(self.arcane.spells_config.keys())
                await channel.send(f"Spell inconnu : `{skill_name}`. Disponibles : {available_skills}")
                return

            task = skill_instructions or f"Execution isolee de /{skill_name} sur la forge {forge_name}"
            thread = await self._create_forge_thread(forge_name, f"/{skill_name} \u2014 {task[:60]}")

            await self.arcane.enqueue_circle_spell(forge_name, skill_name, task, instructions=skill_instructions)
            msg = f"\U0001f9e0 **Circle {forge_name}** \u2192 skill `/{skill_name}` accepte"
            await channel.send(msg)
            if thread:
                await self._send_to_target(msg, thread)

        else:
            # Everything else is treated as the task description
            task = f"{action} {args}".strip() if action else rest.strip()
            if not task:
                await channel.send(f"Usage : `!{forge_name} <description de la tache>`")
                return

            thread = await self._create_forge_thread(forge_name, task)

            position = await self.arcane.enqueue_circle(forge_name, task)
            forge = self.arcane.circles[forge_name]
            workflow_str = " \u2192 ".join(f"`/{s}`" for s in forge.workflow)

            msg = f"\U0001f9e0 **Circle {forge_name}** \u2192 tache acceptee\n\U0001f4cb Workflow : {workflow_str}\n\U0001f4dd Tache : {task}"
            await channel.send(msg)
            if thread:
                await self._send_to_target(msg, thread)

    # ------------------------------------------------------------------
    # Message handler (command routing + feedback injection)
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return

        if message.content.startswith("!"):
            # Extract the first word after !
            raw = message.content[1:].strip()
            word = raw.split()[0] if raw else ""

            if word in self.arcane.circles:
                # Dynamic forge command: !dev, !bugfix, !sentry, etc.
                rest = raw[len(word):].strip()
                try:
                    await self._handle_forge_command(message, word, rest)
                except Exception as exc:
                    logger.error("Erreur commande forge %s: %s", word, exc, exc_info=True)
                    await message.channel.send(f"\u274c Erreur : {exc}")
                return

            # Not a forge command — let discord.py handle it (!crew, etc.)
            await self.process_commands(message)
            return

        # Free-form message (no !) — inject as feedback
        # Check if message is in an active forge thread -> targeted feedback
        for forge_name, thread in self._forge_threads.items():
            if message.channel.id == thread.id:
                forge = self.arcane.circles.get(forge_name)
                if forge and forge.state["status"] in ("running", "paused"):
                    self.arcane.inject_feedback(forge_name, message.content)
                    await message.add_reaction("\U0001f4dd")
                break
        else:
            # Message in a channel (not a thread) -> legacy behavior
            channel_ids = {ch.id for ch in self._channels.values()}
            if message.channel.id in channel_ids and self.arcane.task_running:
                for forge in self.arcane.circles.values():
                    if forge.state["status"] in ("running", "paused"):
                        if forge.name not in self._forge_threads:
                            self.arcane.inject_feedback(forge.name, message.content)
                            await message.add_reaction("\U0001f4dd")
                            break


# ------------------------------------------------------------------
# Slash commands registration
# ------------------------------------------------------------------

def _register_slash_commands(bot_instance: ArcaneBot) -> None:
    """Register slash commands on the bot's command tree."""
    tree = bot_instance.tree

    async def _forge_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        forges = list(bot_instance.arcane.circles.keys())
        return [
            app_commands.Choice(name=f, value=f)
            for f in forges if current.lower() in f.lower()
        ][:25]

    async def _skill_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        skills = list(bot_instance.arcane.spells_config.keys())
        return [
            app_commands.Choice(name=s, value=s)
            for s in skills if current.lower() in s.lower()
        ][:25]

    @tree.command(name="forge", description="Lancer un workflow sur une forge")
    @app_commands.describe(
        forge_name="Nom de la forge",
        task="Description de la tache ou URL de MR",
    )
    @app_commands.autocomplete(forge_name=_forge_autocomplete)
    async def slash_forge(interaction: discord.Interaction, forge_name: str, task: str) -> None:
        if forge_name not in bot_instance.arcane.circles:
            await interaction.response.send_message(f"Circle inconnu : `{forge_name}`", ephemeral=True)
            return
        thread = await bot_instance._create_forge_thread(forge_name, task)
        await bot_instance.arcane.enqueue_circle(forge_name, task)
        forge = bot_instance.arcane.circles[forge_name]
        workflow_str = " \u2192 ".join(f"`/{s}`" for s in forge._all_skill_names())
        msg = f"\U0001f9e0 **Circle {forge_name}** \u2192 tache acceptee\n\U0001f4cb Workflow : {workflow_str}\n\U0001f4dd Tache : {task}"
        await interaction.response.send_message(msg)
        if thread:
            await bot_instance._send_to_target(msg, thread)

    @tree.command(name="skill", description="Executer un skill isole sur une forge")
    @app_commands.describe(
        forge_name="Nom de la forge",
        skill_name="Nom du skill",
        instructions="Instructions optionnelles",
    )
    @app_commands.autocomplete(forge_name=_forge_autocomplete, skill_name=_skill_autocomplete)
    async def slash_skill(interaction: discord.Interaction, forge_name: str, skill_name: str, instructions: Optional[str] = None) -> None:
        if forge_name not in bot_instance.arcane.circles:
            await interaction.response.send_message(f"Circle inconnu : `{forge_name}`", ephemeral=True)
            return
        if skill_name not in bot_instance.arcane.spells_config:
            await interaction.response.send_message(f"Spell inconnu : `{skill_name}`", ephemeral=True)
            return
        task = instructions or f"Execution isolee de /{skill_name}"
        thread = await bot_instance._create_forge_thread(forge_name, f"/{skill_name} \u2014 {task[:60]}")
        await bot_instance.arcane.enqueue_circle_spell(forge_name, skill_name, task, instructions=instructions)
        msg = f"\U0001f9e0 **Circle {forge_name}** \u2192 skill `/{skill_name}` accepte"
        await interaction.response.send_message(msg)
        if thread:
            await bot_instance._send_to_target(msg, thread)

    arcane_group = app_commands.Group(name="arcane", description="Commandes globales Arcane")

    @arcane_group.command(name="status", description="Etat global de Arcane")
    async def slash_crew_status(interaction: discord.Interaction) -> None:
        status = bot_instance.arcane.get_global_status()
        await interaction.response.send_message(status)

    @arcane_group.command(name="forges", description="Liste des forges disponibles")
    async def slash_crew_forges(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot_instance.arcane.list_circles())

    @arcane_group.command(name="skills", description="Liste des skills disponibles")
    async def slash_crew_skills(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot_instance.arcane.list_spells())

    @arcane_group.command(name="metrics", description="Metriques d'execution")
    async def slash_crew_metrics(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot_instance.arcane.get_metrics())

    @arcane_group.command(name="reload", description="Recharger la configuration")
    async def slash_crew_reload(interaction: discord.Interaction) -> None:
        result = bot_instance.arcane.reload_config()
        await interaction.response.send_message(f"\U0001f504 {result}")

    tree.add_command(arcane_group)


bot = ArcaneBot()


# ------------------------------------------------------------------
# !crew commands (global)
# ------------------------------------------------------------------

@bot.command(name="arcane")
async def cmd_crew(ctx: commands.Context, subcommand: str = "status", *, args: str = "") -> None:  # type: ignore[type-arg]
    sub = subcommand.lower()

    if sub == "status":
        await bot._send_to_channel(bot.arcane.get_global_status())
    elif sub == "forges":
        await bot._send_to_channel(bot.arcane.list_circles())
    elif sub == "skills":
        await bot._send_to_channel(bot.arcane.list_spells())
    elif sub == "metrics":
        await bot._send_to_channel(bot.arcane.get_metrics())
    elif sub == "reload":
        result = bot.arcane.reload_config()
        await ctx.send(f"\U0001f504 **Arcane** \u2192 {result}")
    elif sub == "reset":
        bot.arcane.reset_all()
        await ctx.send("\U0001f9e0 **Arcane** \u2192 Toutes les forges ont ete reinitialisees.")
    elif sub == "sentry":
        action = args.strip().lower() if args.strip() else "status"
        if action == "check":
            await ctx.send("\U0001f441 **Sentry** \u2192 Verification manuelle en cours...")
            count = await bot.arcane.run_sentry_check()
            if count == 0:
                await ctx.send("\U0001f441 **Sentry** \u2192 Aucune nouvelle erreur")
        elif action == "start":
            await bot.arcane.start_sentry_monitor()
            await ctx.send("\U0001f441 **Sentry Monitor** \u2192 Demarre")
        elif action == "stop":
            await bot.arcane.stop_sentry_monitor()
            await ctx.send("\U0001f441 **Sentry Monitor** \u2192 Arrete")
        elif action == "status":
            if bot.arcane.sentry_monitor:
                await ctx.send(bot.arcane.sentry_monitor.status)
            else:
                await ctx.send("\U0001f441 **Sentry Monitor** \u2014 non configure (sentry_monitor.enabled: false)")
        else:
            await ctx.send("Usage : `!crew sentry [check|start|stop|status]`")
    else:
        await ctx.send(f"Sous-commande inconnue : `{sub}`. Utilisez `status`, `forges`, `skills` ou `reset`.")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:  # type: ignore[type-arg]
    # Ignore command-not-found since forge commands are handled in on_message
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error("Erreur commande: %s", error, exc_info=error)
    await ctx.send(f"\u274c Erreur : {error}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not DISCORD_TOKEN or DISCORD_TOKEN == "xxx":
        logger.error("Configurez DISCORD_BOT_TOKEN dans .env")
        return

    logger.info("Demarrage du bot Arcane")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
