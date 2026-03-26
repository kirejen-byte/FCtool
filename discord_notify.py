"""
Discord Webhook Notification System
Sends rich embed notifications to Discord via webhooks.
"""

import requests
from datetime import datetime, timezone
from rate_limiter import rate_limit


class DiscordNotifier:
    """Sends notifications to a Discord channel via webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"
        self._session.headers["User-Agent"] = "FCTool/1.0"
        self._status_message_id: str | None = None

    def send_message(self, content: str) -> bool:
        """Send a simple text message."""
        if not self.webhook_url:
            return False
        try:
            rate_limit("discord")
            resp = self._session.post(self.webhook_url, json={"content": content})
            return resp.status_code == 204
        except Exception as e:
            print(f"[Discord] Error sending message: {e}")
            return False

    def send_embed(self, title: str, description: str, color: int = 0x00FF00,
                   fields: list[dict] | None = None, url: str | None = None) -> bool:
        """Send a rich embed message."""
        if not self.webhook_url:
            return False

        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "FCTool"},
        }
        if url:
            embed["url"] = url
        if fields:
            embed["fields"] = fields

        try:
            rate_limit("discord")
            resp = self._session.post(
                self.webhook_url,
                json={"embeds": [embed]}
            )
            return resp.status_code == 204
        except Exception as e:
            print(f"[Discord] Error sending embed: {e}")
            return False

    def send_or_update_status(self, online: bool) -> bool:
        """
        Send a status embed, or update the existing one.
        This avoids spamming — only one status message exists at a time.
        """
        if not self.webhook_url:
            return False

        color = 0x00FF00 if online else 0xFF0000
        status_text = "ONLINE" if online else "OFFLINE"
        embed = {
            "title": f"FCTool Monitor: {status_text}",
            "description": f"zKillboard monitor is **{status_text.lower()}**.",
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Last updated"},
        }

        try:
            if self._status_message_id:
                # Edit existing message
                resp = self._session.patch(
                    f"{self.webhook_url}/messages/{self._status_message_id}",
                    json={"embeds": [embed]}
                )
                if resp.ok:
                    return True
                # If edit failed (message deleted?), fall through to create new

            # Create new message (with ?wait=true to get the message ID back)
            resp = self._session.post(
                f"{self.webhook_url}?wait=true",
                json={"embeds": [embed]}
            )
            if resp.ok:
                data = resp.json()
                self._status_message_id = data.get("id")
                return True
        except Exception as e:
            print(f"[Discord] Error with status message: {e}")
        return False

    def notify_xup_ready(self, count: int, threshold: int):
        """Notify that x-ups have hit threshold."""
        self.send_embed(
            title="FLEET READY",
            description=f"**{count}** x-ups received (threshold: {threshold})",
            color=0x00FF00,  # Green
            fields=[
                {"name": "Status", "value": "Ready to fire!", "inline": True},
            ]
        )

    def notify_xup_fire(self, fire_count: int):
        """Notify that FIRE was called."""
        self.send_embed(
            title="FIRE CALLED",
            description=f"FC called fire! (fire #{fire_count})",
            color=0xFF4500,  # Orange-red
        )

    def notify_zkill_alert(self, system_name: str, region_name: str,
                           kill_count: int, pilots_on_field: int,
                           value_millions: float, zkill_url: str,
                           capitals_involved: bool = False,
                           dotlan_url: str = "",
                           route_info: str = "",
                           capital_breakdown: dict[str, int] | None = None,
                           is_update: bool = False,
                           br_url: str = "",
                           zkill_related_url: str = "",
                           warbeacon_url: str = "",
                           top_alliances: list[tuple[str, int]] | None = None):
        """Notify about a detected engagement on zKillboard."""
        if is_update:
            title = f"FIGHT GROWING: {system_name}"
            if capitals_involved:
                title = f"CAPITAL FIGHT GROWING: {system_name}"
        else:
            title = f"FIGHT DETECTED: {system_name}"
            if capitals_involved:
                title = f"CAPITAL FIGHT: {system_name}"
        desc = f"Engagement detected in **{system_name}** ({region_name})"
        if capitals_involved:
            cap_detail = ""
            if capital_breakdown:
                parts = [f"{count} {cls}" for cls, count in
                         sorted(capital_breakdown.items(),
                                key=lambda x: x[1], reverse=True)]
                cap_detail = f" ({', '.join(parts)})"
            desc += f"\n**CAPITAL SHIPS ON FIELD**{cap_detail}"
        fields = [
            {"name": "Pilots on Field", "value": str(pilots_on_field), "inline": True},
            {"name": "Kills", "value": str(kill_count), "inline": True},
            {"name": "Value", "value": f"{value_millions:.0f}M ISK", "inline": True},
            {"name": "Region", "value": region_name, "inline": True},
        ]
        if capitals_involved:
            cap_val = "YES"
            if capital_breakdown:
                cap_val = ", ".join(f"{count} {cls}" for cls, count in
                                    sorted(capital_breakdown.items(),
                                           key=lambda x: x[1], reverse=True))
            fields.append({"name": "Capitals", "value": cap_val, "inline": True})

        if top_alliances:
            alliance_lines = ", ".join(f"{name} ({count})" for name, count in top_alliances)
            fields.append({"name": "Involved Alliances", "value": alliance_lines, "inline": False})

        # Links
        links = f"[zKillboard]({zkill_url})"
        if dotlan_url:
            links += f" | [Dotlan]({dotlan_url})"
        if zkill_related_url:
            links += f" | [Related Kills]({zkill_related_url})"
        if warbeacon_url:
            links += f" | [WarBeacon BR]({warbeacon_url})"
        # Legacy fallback for br_url
        elif br_url:
            links += f" | [Battle Report]({br_url})"
        fields.append({"name": "Links", "value": links, "inline": False})

        if route_info:
            fields.append({"name": "Route from staging", "value": route_info, "inline": False})

        self.send_embed(
            title=title,
            description=desc,
            color=0xFF0000,  # Red
            url=zkill_url,
            fields=fields,
        )
