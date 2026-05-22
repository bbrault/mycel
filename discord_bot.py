from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from forge import extract_json
from mycel import Mycel
from message_bus import Message

load_dotenv()

logger = logging.getLogger("mycel.discord")

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


class MonitorAlertView(discord.ui.View):
    """One button per actionable issue from a Sentry/Aikido monitor alert.

    Clicking a button enqueues the corresponding remediation forge with the
    issue id+title as task. Used when REMEDIATION_AUTO_FIX=false so the
    human stays in the loop before /fix runs.
    """

    def __init__(self, orchestrator: "Mycel", forge_name: str, issues: List[Dict[str, str]]) -> None:
        super().__init__(timeout=86400)  # 24h — issues stay actionable for a day
        self.orchestrator = orchestrator
        self.forge_name = forge_name
        for issue in issues:
            issue_id = issue.get("id", "?")
            label = f"🔧 /fix {issue_id}"[:80]
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"fix_{forge_name}_{issue_id}",
            )
            btn.callback = self._make_callback(issue.get("task", issue_id))
            self.add_item(btn)

    def _make_callback(self, task: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True)
            try:
                await self.orchestrator.enqueue_forge(self.forge_name, task)
                await interaction.followup.send(
                    f"⚒️ **Forge {self.forge_name}** → enqueued `/fix` for `{task}`"
                )
            except Exception as exc:
                await interaction.followup.send(f"❌ Could not enqueue: {exc}")
        return _cb


class ForgeControlView(discord.ui.View):
    """Interactive buttons when a forge pauses (Resume / Retry / Reset)."""

    def __init__(self, orchestrator: Mycel, forge_name: str, show_push: bool = False) -> None:
        super().__init__(timeout=3600)  # 1h timeout
        self.orchestrator = orchestrator
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

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.blurple, emoji="▶️")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        await self.orchestrator.resume_forge(self.forge_name)
        await interaction.response.send_message(
            f"▶️ **Forge {self.forge_name}** → workflow resumed",
        )
        self.stop()

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.blurple, emoji="\U0001f504")
    async def retry_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        await self.orchestrator.retry_forge(self.forge_name)
        await interaction.response.send_message(
            f"\U0001f504 **Forge {self.forge_name}** → current step retried",
        )
        self.stop()

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.red, emoji="\U0001f5d1️")
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        self.orchestrator.reset_forge(self.forge_name)
        await interaction.response.send_message(
            f"\U0001f5d1️ **Forge {self.forge_name}** → reset",
        )
        self.stop()

    async def _push_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        forge = self.orchestrator.forges.get(self.forge_name)
        if not forge:
            await interaction.followup.send("❌ Forge not found")
            return
        try:
            await forge._finalize_git(forge.state.get("current_skill", "implement"))
            await interaction.followup.send(
                f"\U0001f680 **Forge {self.forge_name}** → Push + MR done. See messages above for details.",
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Push failed: {exc}")


class ClaapElaborateModal(discord.ui.Modal):
    """Modal opened after picking an idea: collects optional notes, then resumes
    the discovery workflow at /elaborate (which auto-advances to /plan)."""

    def __init__(
        self,
        orchestrator: "Mycel",
        forge_name: str,
        idea_id: str,
        idea_name: str,
        idea_summary: str,
    ) -> None:
        super().__init__(title=f"Elaborate + Plan — {idea_id}"[:45])
        self.orchestrator = orchestrator
        self.forge_name = forge_name
        self.idea_id = idea_id
        self.idea_name = idea_name
        self.idea_summary = idea_summary

        self.notes = discord.ui.TextInput(
            label="Notes / contraintes / scope (optionnel)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=2000,
            placeholder="Précisions, contraintes, périmètre à respecter…",
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        notes = (self.notes.value or "").strip()
        idea_label = f"{self.idea_id} ({self.idea_name})" if self.idea_name else self.idea_id
        instructions_parts = [
            f"Approfondis l'idée {idea_label} identifiée par /claap-discovery "
            f"(le JSON complet est dans previous_output). "
            f"Produis les specs fonctionnelles puis le plan technique pour CETTE idée uniquement.",
        ]
        if self.idea_summary:
            instructions_parts.append(f"Résumé de l'idée : {self.idea_summary}")
        if notes:
            instructions_parts.append(f"Notes utilisateur :\n{notes}")
        instructions = "\n\n".join(instructions_parts)

        try:
            await self.orchestrator.resume_forge(self.forge_name, instructions=instructions)
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Could not resume forge: {exc}", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✨ **Forge {self.forge_name}** → /elaborate + /plan lancés pour `{self.idea_id}`",
        )


class ClaapDiscoveryView(discord.ui.View):
    """Post-/claap-discovery pause: select an idea → modal → resume ritual.

    Falls back to plain Resume/Retry/Reset if no ideas are available.
    """

    def __init__(
        self,
        orchestrator: "Mycel",
        forge_name: str,
        ideas: List[Dict[str, Any]],
        top_pick_id: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=86400)
        self.orchestrator = orchestrator
        self.forge_name = forge_name
        self._idea_meta: Dict[str, Dict[str, str]] = {}

        options: List[discord.SelectOption] = []
        for idea in ideas[:25]:
            idea_id = str(idea.get("id") or "").strip() or f"IDEA-{len(options) + 1}"
            name = str(idea.get("name") or "").strip() or "(sans titre)"
            priority = str(idea.get("priority") or "?")
            effort = str(idea.get("effort") or "?")
            roi = (idea.get("roi") or {}).get("roi_score")
            roi_str = f", ROI {roi}" if roi not in (None, "") else ""
            label = f"{idea_id} — {name}"[:100]
            description = f"priority {priority} · effort {effort}{roi_str}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=idea_id,
                    description=description,
                    default=(top_pick_id is not None and idea_id == top_pick_id),
                )
            )
            self._idea_meta[idea_id] = {
                "name": name,
                "summary": str(idea.get("solution") or idea.get("problem") or "")[:500],
            }

        if options:
            select = discord.ui.Select(
                placeholder="✨ Choisir une idée pour /elaborate + /plan",
                options=options,
                custom_id=f"claap_idea_{forge_name}",
                min_values=1,
                max_values=1,
            )
            select.callback = self._on_select  # type: ignore[assignment]
            self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        idea_id = (interaction.data or {}).get("values", [None])[0]  # type: ignore[index]
        if not idea_id:
            await interaction.response.send_message("❌ No idea selected", ephemeral=True)
            return
        meta = self._idea_meta.get(idea_id, {})
        modal = ClaapElaborateModal(
            self.orchestrator,
            self.forge_name,
            idea_id=idea_id,
            idea_name=meta.get("name", ""),
            idea_summary=meta.get("summary", ""),
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.blurple, emoji="▶️", row=1)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        await self.orchestrator.resume_forge(self.forge_name)
        await interaction.response.send_message(
            f"▶️ **Forge {self.forge_name}** → workflow resumed",
        )
        self.stop()

    @discord.ui.button(label="Retry", style=discord.ButtonStyle.blurple, emoji="\U0001f504", row=1)
    async def retry_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        await self.orchestrator.retry_forge(self.forge_name)
        await interaction.response.send_message(
            f"\U0001f504 **Forge {self.forge_name}** → current step retried",
        )
        self.stop()

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.red, emoji="\U0001f5d1️", row=1)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        self.orchestrator.reset_forge(self.forge_name)
        await interaction.response.send_message(
            f"\U0001f5d1️ **Forge {self.forge_name}** → reset",
        )
        self.stop()


def _build_claap_view(orchestrator: "Mycel", forge_name: str) -> Optional[ClaapDiscoveryView]:
    """Read the latest /claap-discovery output from the forge and build the view.

    Returns None if the output is missing or doesn't contain feature_ideas.
    """
    forge = orchestrator.forges.get(forge_name)
    if not forge:
        return None
    output = forge.state.get("step_outputs", {}).get("claap-discovery")
    if not output:
        return None
    data = extract_json(output)
    if not data:
        return None
    ideas = data.get("feature_ideas") or []
    if not isinstance(ideas, list) or not ideas:
        return None
    top_pick_id = ((data.get("top_pick") or {}).get("idea_id")) or None
    return ClaapDiscoveryView(orchestrator, forge_name, ideas, top_pick_id=top_pick_id)


def _forge_configs(orchestrator: Mycel) -> Dict[str, Dict]:
    """Return the forges section from config, supporting legacy `circles:` key."""
    return orchestrator.config.get("forges", orchestrator.config.get("circles", {})) or {}


class MycelBot(commands.Bot):
    """Discord bot that drives Mycel."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.orchestrator = Mycel()
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
        self.orchestrator.bus.subscribe(self._on_bus_message)
        await self.orchestrator.bus.start()
        await self.orchestrator.start_worker()
        await self.orchestrator.start_sentry_monitor()
        await self.orchestrator.start_aikido_monitor()
        # Register slash commands
        _register_slash_commands(self)
        try:
            await self.tree.sync()
            logger.info("Slash commands synced")
        except Exception as exc:
            logger.warning("Could not sync slash commands: %s", exc)

    async def on_ready(self) -> None:
        logger.info("Connected as %s", self.user)

        # Collect all channel names from forge configs + default
        channel_names = {DEFAULT_CHANNEL}
        for forge_cfg in _forge_configs(self.orchestrator).values():
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
                logger.info("Channel #%s found", name)
            else:
                logger.warning("Channel #%s not found — create it in Discord", name)

        if self.agents_channel:
            await self._send_welcome()
            await self._update_dashboard()

    # ------------------------------------------------------------------
    # Channel & thread routing
    # ------------------------------------------------------------------

    def _get_forge_channel_name(self, forge_name: str) -> str:
        """Return the Discord channel name configured for a forge."""
        forge_cfg = _forge_configs(self.orchestrator).get(forge_name, {})
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
        existing = self._forge_threads.get(forge_name)
        if existing is not None:
            try:
                thread = existing.guild.get_thread(existing.id)  # type: ignore[union-attr]
                if thread and not thread.archived:
                    logger.info("Reusing thread for forge %s: %s", forge_name, thread.name)
                    return thread
            except Exception:
                pass
            self._forge_threads.pop(forge_name, None)

        channel = self._get_forge_channel(forge_name)
        if channel is None:
            return None

        thread_name = f"⚒️ {forge_name} — {task[:80]}"
        if len(thread_name) > 100:
            thread_name = thread_name[:97] + "..."

        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,  # 24h
            )
            self._forge_threads[forge_name] = thread
            logger.info("Thread created for forge %s: %s", forge_name, thread_name)
            return thread
        except Exception as exc:
            logger.error("Thread creation error for forge %s: %s", forge_name, exc)
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
                    logger.error("Discord send error: %s", exc)

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
            logger.warning("Bus message ignored (no target): %s", message.content[:80])
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
                logger.debug("Streaming edit failed: %s", exc)
            return

        # Clean up streaming message when spell finishes (non-streaming message arrives)
        if message.forge_name:
            for sid in list(self._streaming_messages):
                if sid.startswith(f"{message.forge_name}_"):
                    self._streaming_messages.pop(sid, None)

        # If this is a pause message, send with interactive buttons
        if message.data.get("paused") and message.forge_name:
            spell_name = message.data.get("skill_name", "")
            spell_cfg = self.orchestrator.spells_config.get(spell_name, {})
            show_push = spell_cfg.get("git_prepare", False)
            view: discord.ui.View
            if spell_name == "claap-discovery":
                claap_view = _build_claap_view(self.orchestrator, message.forge_name)
                view = claap_view if claap_view is not None else ForgeControlView(
                    self.orchestrator, message.forge_name, show_push=show_push
                )
            else:
                view = ForgeControlView(self.orchestrator, message.forge_name, show_push=show_push)
            try:
                await target.send(message.content, view=view)
            except Exception as exc:
                logger.error("Discord button send error: %s", exc)
                await self._send_to_target(message.content, target)
        elif message.data.get("monitor_alert"):
            forge = message.data.get("forge", message.forge_name or "")
            issues = message.data.get("actionable_issues") or []
            if forge and issues and forge in self.orchestrator.forges:
                view = MonitorAlertView(self.orchestrator, forge, issues)
                # Buttons must travel with the last chunk; send chunks then the
                # final one with the view attached.
                chunks = chunk_message(message.content)
                head, last = chunks[:-1], chunks[-1]
                for chunk in head:
                    if chunk.strip():
                        try:
                            await target.send(chunk)
                        except Exception as exc:
                            logger.error("Discord send error: %s", exc)
                try:
                    await target.send(last, view=view)
                except Exception as exc:
                    logger.error("Discord button send error: %s", exc)
                    await self._send_to_target(message.content, target)
            else:
                await self._send_to_target(message.content, target)
        else:
            await self._send_to_target(message.content, target)

        # Upload output file as attachment if present
        output_file = message.data.get("output_file")
        if output_file and os.path.isfile(output_file):
            try:
                file = discord.File(output_file, filename=os.path.basename(output_file))
                await target.send(
                    f"\U0001f4ce File: `{os.path.basename(output_file)}`",
                    file=file,
                )
            except Exception as exc:
                logger.error("Discord file upload error: %s", exc)

    # ------------------------------------------------------------------
    # Welcome message
    # ------------------------------------------------------------------

    async def _update_dashboard(self) -> None:
        """Update (or create) the pinned dashboard message in #agents."""
        if self.agents_channel is None:
            return

        lines = ["\U0001f344 **Mycel dashboard**\n"]
        for name, forge in self.orchestrator.forges.items():
            lines.append(forge.progress_bar)

        queue_total = self.orchestrator.queue_size
        if queue_total > 0:
            lines.append(f"\n\U0001f4cb Queue: {queue_total} task(s)")

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
            self._dashboard_msg = await self.agents_channel.send(content)
        except Exception as exc:
            logger.debug("Dashboard update failed: %s", exc)

    async def _send_welcome(self) -> None:
        forges_list = "\n".join(
            f"• `!{name}` — {forge.description} (#{self._get_forge_channel_name(name)})"
            for name, forge in self.orchestrator.forges.items()
        )
        check = "✅"
        cross = "❌"
        familiars = ", ".join(
            f"{k}: {check if v else cross}"
            for k, v in self.orchestrator.familiar_status.items()
        )

        channels_str = ", ".join(f"#{name}" for name in sorted(self._channels.keys()))

        welcome = (
            "\U0001f344 **Mycel online**\n\n"
            f"**Agents:** {familiars}\n"
            f"**Channels:** {channels_str}\n\n"
            f"**Forges:**\n{forges_list}\n\n"
            "**Commands:**\n"
            "• `!<forge> <description>` (e.g. `!dev`) → start workflow (creates a thread)\n"
            "• `!<forge> step <name> [instructions]` → run a single step\n"
            "• `!<forge> from <step>` → resume from a step\n"
            "• `!<forge> resume [instructions]` → resume after a pause\n"
            "• `!<forge> retry [instructions]` → retry current step\n"
            "• `!<forge> status` → progress for this forge\n"
            "• `!<forge> sync` → refresh git + GitLab (MR/pipeline) state\n"
            "• `!<forge> log [N]` → last N bus messages\n"
            "• `!<forge> reset` → reset this forge\n"
            "• `!mycel status` → global status\n"
            "• `!mycel forges` → list configured forges\n"
            "• `!mycel steps` → list available steps\n"
            "• `!mycel mcp` → check MCP server health\n"
            "• `!mycel sentry [check|start|stop|status]` → Sentry monitor\n"
            "• `!mycel aikido [check|start|stop|status]` → Aikido monitor\n"
            "• `!mycel reset` → reset all forges\n"
            "• `!mycel reset metrics` → reset all forges + zero counters\n\n"
            "Reply in an active thread to add context for the next step."
        )
        await self._send_to_channel(welcome)

    # ------------------------------------------------------------------
    # Dynamic forge command handler
    # ------------------------------------------------------------------

    def _check_permission(self, member: discord.Member, forge_name: str) -> bool:
        """Check if a member has permission to use a forge based on role config."""
        permissions = self.orchestrator.config.get("permissions", {})
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
                await channel.send(f"\U0001f6ab You are not allowed to use forge **{forge_name}**.")
                return

        parts = rest.split(maxsplit=1) if rest else []
        action = parts[0] if parts else ""
        action_lower = action.lower()
        args = parts[1] if len(parts) > 1 else ""

        if action_lower == "status":
            status = await self.orchestrator.get_forge_status(forge_name)
            target = self._get_send_target(forge_name)
            if target:
                await self._send_to_target(status, target)
            else:
                await channel.send(status)

        elif action_lower == "sync":
            target = self._get_send_target(forge_name)
            ack = f"🔄 **Forge {forge_name}** → syncing with GitLab…"
            if target:
                await self._send_to_target(ack, target)
            else:
                await channel.send(ack)
            status = await self.orchestrator.sync_forge(forge_name)
            if target:
                await self._send_to_target(status, target)
            else:
                await channel.send(status)

        elif action_lower == "reset":
            if args.strip().lower() == "metrics":
                self.orchestrator.reset_forge_metrics(forge_name)
                self._forge_threads.pop(forge_name, None)
                await channel.send(f"\U0001f4ca **Forge {forge_name}** → metrics reset (counters zeroed).")
            else:
                self.orchestrator.reset_forge(forge_name)
                self._forge_threads.pop(forge_name, None)
                await channel.send(f"⚒️ **Forge {forge_name}** → reset.")

        elif action_lower == "log":
            limit = 20
            if args.strip().isdigit():
                limit = int(args.strip())
            log_text = self.orchestrator.get_forge_log(forge_name, limit=limit)
            target = self._get_send_target(forge_name)
            if target:
                await self._send_to_target(log_text, target)
            else:
                await channel.send(log_text)

        elif action_lower == "from":
            if not args:
                await channel.send(f"Usage: `!{forge_name} from <step> [instructions]`")
                return
            from_parts = args.split(maxsplit=1)
            from_spell = from_parts[0]
            from_instructions = from_parts[1] if len(from_parts) > 1 else None

            forge = self.orchestrator.forges[forge_name]
            if from_spell not in forge._all_skill_names():
                available = forge.format_workflow()
                await channel.send(f"Step `/{from_spell}` is not in this workflow. Available: {available}")
                return

            task = forge.state.get("task") or "resume"
            thread = await self._create_forge_thread(forge_name, task)

            idx = forge._find_skill_workflow_index(from_spell)

            def _render_step(step) -> str:
                if isinstance(step, dict) and "parallel" in step:
                    return "(" + " | ".join(f"`/{n}`" for n in step["parallel"]) + ")"
                return f"`/{step}`"

            kept = forge.workflow[:idx]
            rerun = forge.workflow[idx:]
            kept_str = ", ".join(f"✅ {_render_step(s)}" for s in kept) if kept else "(none)"
            rerun_str = " → ".join(_render_step(s) for s in rerun)

            await self.orchestrator.enqueue_from_spell(forge_name, from_spell, from_instructions)

            msg = (
                f"\U0001f504 ⚒️ **Forge {forge_name}** → resuming from `/{from_spell}`\n"
                f"\U0001f4e6 Kept: {kept_str}\n"
                f"\U0001f501 Re-runs: {rerun_str}"
            )
            await channel.send(msg)
            if thread:
                await self._send_to_target(msg, thread)

        elif action_lower == "resume":
            if self.orchestrator.forges[forge_name].state["status"] != "paused":
                await channel.send(f"⚒️ **Forge {forge_name}** is not paused.")
                return
            await self.orchestrator.resume_forge(forge_name, instructions=args or None)
            await channel.send(f"▶️ **Forge {forge_name}** → workflow resumed")

        elif action_lower == "abort":
            aborted = self.orchestrator.abort_forge(forge_name)
            if aborted:
                await channel.send(f"\U0001f6d1 **Forge {forge_name}** → current step aborted. Use `!{forge_name} from <step>` to continue.")
            else:
                await channel.send(f"⚒️ **Forge {forge_name}** is not running.")

        elif action_lower == "retry":
            if self.orchestrator.forges[forge_name].state["status"] not in ("paused", "failed", "error"):
                await channel.send(f"⚒️ **Forge {forge_name}** is not paused or in error state.")
                return
            await self.orchestrator.retry_forge(forge_name, instructions=args or None)
            await channel.send(f"\U0001f504 **Forge {forge_name}** → current step retried")

        elif action_lower in ("spell", "skill", "step"):
            spell_parts = args.split(maxsplit=1) if args else []
            if not spell_parts:
                await channel.send(f"Usage: `!{forge_name} step <step_name> [instructions]`")
                return
            spell_name = spell_parts[0]
            spell_instructions = spell_parts[1] if len(spell_parts) > 1 else None

            if spell_name not in self.orchestrator.spells_config:
                available = ", ".join(self.orchestrator.spells_config.keys())
                await channel.send(f"Unknown step: `{spell_name}`. Available: {available}")
                return

            task = spell_instructions or f"Isolated run of /{spell_name} on forge {forge_name}"
            thread = await self._create_forge_thread(forge_name, f"/{spell_name} — {task[:60]}")

            await self.orchestrator.enqueue_forge_spell(forge_name, spell_name, task, instructions=spell_instructions)
            msg = f"⚒️ **Forge {forge_name}** → step `/{spell_name}` enqueued"
            await channel.send(msg)
            if thread:
                await self._send_to_target(msg, thread)

        else:
            # Everything else is treated as the task description
            task = f"{action} {args}".strip() if action else rest.strip()
            if not task:
                await channel.send(f"Usage: `!{forge_name} <task description>`")
                return

            thread = await self._create_forge_thread(forge_name, task)

            await self.orchestrator.enqueue_forge(forge_name, task)
            forge = self.orchestrator.forges[forge_name]
            ritual_str = forge.format_workflow()

            msg = f"⚒️ **Forge {forge_name}** → task accepted\n\U0001f4cb Workflow: {ritual_str}\n\U0001f4dd Task: {task}"
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

            if word in self.orchestrator.forges:
                # Dynamic forge command: !dev, !bugfix, !sentry, etc.
                rest = raw[len(word):].strip()
                try:
                    await self._handle_forge_command(message, word, rest)
                except Exception as exc:
                    logger.error("Forge command error %s: %s", word, exc, exc_info=True)
                    await message.channel.send(f"❌ Error: {exc}")
                return

            # Not a forge command — let discord.py handle it (!mycel, !dispatch alias, etc.)
            await self.process_commands(message)
            return

        # Free-form message (no !) — inject as feedback
        for forge_name, thread in self._forge_threads.items():
            if message.channel.id == thread.id:
                forge = self.orchestrator.forges.get(forge_name)
                if forge and forge.state["status"] in ("running", "paused"):
                    self.orchestrator.inject_feedback(forge_name, message.content)
                    await message.add_reaction("\U0001f4dd")
                break
        else:
            channel_ids = {ch.id for ch in self._channels.values()}
            if message.channel.id in channel_ids and self.orchestrator.task_running:
                for forge in self.orchestrator.forges.values():
                    if forge.state["status"] in ("running", "paused"):
                        if forge.name not in self._forge_threads:
                            self.orchestrator.inject_feedback(forge.name, message.content)
                            await message.add_reaction("\U0001f4dd")
                            break


# ------------------------------------------------------------------
# Slash commands registration
# ------------------------------------------------------------------

def _register_slash_commands(bot_instance: MycelBot) -> None:
    """Register slash commands on the bot's command tree."""
    tree = bot_instance.tree

    async def _forge_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        forges = list(bot_instance.orchestrator.forges.keys())
        return [
            app_commands.Choice(name=f, value=f)
            for f in forges if current.lower() in f.lower()
        ][:25]

    async def _spell_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        spells = list(bot_instance.orchestrator.spells_config.keys())
        return [
            app_commands.Choice(name=s, value=s)
            for s in spells if current.lower() in s.lower()
        ][:25]

    @tree.command(name="forge", description="Start a workflow on a forge")
    @app_commands.describe(
        forge_name="Forge name",
        task="Task description or MR URL",
    )
    @app_commands.autocomplete(forge_name=_forge_autocomplete)
    async def slash_forge(interaction: discord.Interaction, forge_name: str, task: str) -> None:
        if forge_name not in bot_instance.orchestrator.forges:
            await interaction.response.send_message(f"Unknown forge: `{forge_name}`", ephemeral=True)
            return
        thread = await bot_instance._create_forge_thread(forge_name, task)
        await bot_instance.orchestrator.enqueue_forge(forge_name, task)
        forge = bot_instance.orchestrator.forges[forge_name]
        ritual_str = forge.format_workflow()
        msg = f"⚒️ **Forge {forge_name}** → task accepted\n\U0001f4cb Workflow: {ritual_str}\n\U0001f4dd Task: {task}"
        await interaction.response.send_message(msg)
        if thread:
            await bot_instance._send_to_target(msg, thread)

    @tree.command(name="spell", description="Run a single step on a forge")
    @app_commands.describe(
        forge_name="Forge name",
        spell_name="Step name (e.g. plan, implement)",
        instructions="Optional instructions",
    )
    @app_commands.autocomplete(forge_name=_forge_autocomplete, spell_name=_spell_autocomplete)
    async def slash_spell(interaction: discord.Interaction, forge_name: str, spell_name: str, instructions: Optional[str] = None) -> None:
        if forge_name not in bot_instance.orchestrator.forges:
            await interaction.response.send_message(f"Unknown forge: `{forge_name}`", ephemeral=True)
            return
        if spell_name not in bot_instance.orchestrator.spells_config:
            await interaction.response.send_message(f"Unknown step: `{spell_name}`", ephemeral=True)
            return
        task = instructions or f"Isolated run of /{spell_name}"
        thread = await bot_instance._create_forge_thread(forge_name, f"/{spell_name} — {task[:60]}")
        await bot_instance.orchestrator.enqueue_forge_spell(forge_name, spell_name, task, instructions=instructions)
        msg = f"⚒️ **Forge {forge_name}** → step `/{spell_name}` enqueued"
        await interaction.response.send_message(msg)
        if thread:
            await bot_instance._send_to_target(msg, thread)

    mycel_group = app_commands.Group(name="mycel", description="Global Mycel commands")

    @mycel_group.command(name="status", description="Global Mycel status")
    async def slash_mycel_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        status = await bot_instance.orchestrator.get_global_status()
        await interaction.followup.send(status)

    @mycel_group.command(name="sync", description="Sync a forge with GitLab (MR + pipeline state)")
    async def slash_mycel_sync(interaction: discord.Interaction, forge_name: str) -> None:
        await interaction.response.defer()
        status = await bot_instance.orchestrator.sync_forge(forge_name)
        await interaction.followup.send(status)

    @mycel_group.command(name="forges", description="List configured forges")
    async def slash_mycel_forges(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot_instance.orchestrator.list_forges())

    @mycel_group.command(name="spells", description="List available steps")
    async def slash_mycel_spells(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot_instance.orchestrator.list_spells())

    @mycel_group.command(name="metrics", description="Execution metrics")
    async def slash_mycel_metrics(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(bot_instance.orchestrator.get_metrics())

    @mycel_group.command(name="reset-metrics", description="Zero all metric counters (state + skill_metrics + run_number)")
    async def slash_mycel_reset_metrics(interaction: discord.Interaction) -> None:
        bot_instance.orchestrator.reset_all_metrics()
        bot_instance._forge_threads.clear()
        await interaction.response.send_message(
            "\U0001f4ca **Mycel** → All metrics counters zeroed (state + skill_metrics + run_number)."
        )

    @mycel_group.command(name="mcp", description="Check MCP server health")
    async def slash_mycel_mcp(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        status = await bot_instance.orchestrator.get_mcp_status()
        chunks = chunk_message(status)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    @mycel_group.command(name="reload", description="Reload configuration from disk")
    async def slash_mycel_reload(interaction: discord.Interaction) -> None:
        result = bot_instance.orchestrator.reload_config()
        await interaction.response.send_message(f"\U0001f504 {result}")

    tree.add_command(mycel_group)


bot = MycelBot()


# ------------------------------------------------------------------
# !mycel commands (global)  — !dispatch is registered as an alias below
# ------------------------------------------------------------------

async def _handle_mycel_subcommand(ctx: commands.Context, subcommand: str, args: str) -> None:
    sub = subcommand.lower()

    if sub == "status":
        await bot._send_to_channel(await bot.orchestrator.get_global_status())
    elif sub in ("forges", "forge", "workflow", "workflows", "circles"):
        await bot._send_to_channel(bot.orchestrator.list_forges())
    elif sub in ("spells", "spell", "skill", "skills", "steps", "step"):
        await bot._send_to_channel(bot.orchestrator.list_spells())
    elif sub == "metrics":
        await bot._send_to_channel(bot.orchestrator.get_metrics())
    elif sub == "reload":
        result = bot.orchestrator.reload_config()
        await ctx.send(f"\U0001f504 **Mycel** → {result}")
    elif sub == "reset":
        if args.strip().lower() == "metrics":
            bot.orchestrator.reset_all_metrics()
            bot._forge_threads.clear()
            await ctx.send("\U0001f4ca **Mycel** → All metrics counters zeroed (state + skill_metrics + run_number).")
        else:
            bot.orchestrator.reset_all()
            await ctx.send("\U0001f344 **Mycel** → All forges have been reset.")
    elif sub == "mcp":
        status = await bot.orchestrator.get_mcp_status()
        await bot._send_to_channel(status)
    elif sub == "sentry":
        action = args.strip().lower() if args.strip() else "status"
        if action == "check":
            await ctx.send("\U0001f441 **Sentry** → Manual check running...")
            count = await bot.orchestrator.run_sentry_check()
            if count == 0:
                await ctx.send("\U0001f441 **Sentry** → No new issues")
        elif action == "start":
            await bot.orchestrator.start_sentry_monitor()
            await ctx.send("\U0001f441 **Sentry Monitor** → Started")
        elif action == "stop":
            await bot.orchestrator.stop_sentry_monitor()
            await ctx.send("\U0001f441 **Sentry Monitor** → Stopped")
        elif action == "status":
            if bot.orchestrator.sentry_monitor:
                await ctx.send(bot.orchestrator.sentry_monitor.status)
            else:
                await ctx.send("\U0001f441 **Sentry Monitor** — not configured (sentry_monitor.enabled: false)")
        else:
            await ctx.send("Usage: `!mycel sentry [check|start|stop|status]`")
    elif sub == "aikido":
        action = args.strip().lower() if args.strip() else "status"
        if action == "check":
            await ctx.send("\U0001f6e1 **Aikido** → Manual check running...")
            count = await bot.orchestrator.run_aikido_check()
            if count == 0:
                await ctx.send("\U0001f6e1 **Aikido** → No new issues")
        elif action == "start":
            await bot.orchestrator.start_aikido_monitor()
            await ctx.send("\U0001f6e1 **Aikido Monitor** → Started")
        elif action == "stop":
            await bot.orchestrator.stop_aikido_monitor()
            await ctx.send("\U0001f6e1 **Aikido Monitor** → Stopped")
        elif action == "status":
            if bot.orchestrator.aikido_monitor:
                await ctx.send(bot.orchestrator.aikido_monitor.status)
            else:
                await ctx.send("\U0001f6e1 **Aikido Monitor** — not configured (aikido_monitor.enabled: false)")
        else:
            await ctx.send("Usage: `!mycel aikido [check|start|stop|status]`")
    else:
        await ctx.send(
            f"Unknown subcommand: `{sub}`. Use `status`, `forges`, `steps`, `mcp`, `metrics`, `reload`, or `reset`."
        )


@bot.command(name="mycel")
async def cmd_mycel(ctx: commands.Context, subcommand: str = "status", *, args: str = "") -> None:  # type: ignore[type-arg]
    await _handle_mycel_subcommand(ctx, subcommand, args)


@bot.command(name="dispatch")
async def cmd_dispatch_alias(ctx: commands.Context, subcommand: str = "status", *, args: str = "") -> None:  # type: ignore[type-arg]
    """Backwards-compat alias for !mycel."""
    await _handle_mycel_subcommand(ctx, subcommand, args)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:  # type: ignore[type-arg]
    # Ignore command-not-found since forge commands are handled in on_message
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error("Command error: %s", error, exc_info=error)
    await ctx.send(f"❌ Error: {error}")


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
        logger.error("Set DISCORD_BOT_TOKEN in .env")
        return

    logger.info("Starting Mycel bot")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
