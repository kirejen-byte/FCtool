"""
FCTool Headless - zKillboard Monitor + Discord Alerts
Lightweight version designed to run 24/7 on a VPS.
No GUI, no chat log scanning — just zkill monitoring and Discord notifications.

Usage:
    python fc_headless.py                  # Run with config.json
    python fc_headless.py --config my.json # Custom config
"""

import argparse
import json
import os
import sys
import signal
import time
from datetime import datetime, timezone

from zkill_monitor import ZKillMonitor, KillAlert, resolve_name
from discord_notify import DiscordNotifier
from jump_range import search_system, get_stargate_route


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"[ERROR] Config not found: {path}")
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def get_route_info(staging_system: str, target_system: str) -> str:
    """Get gate route info from staging to target system."""
    if not staging_system:
        return ""
    try:
        origin_id = search_system(staging_system)
        dest_id = search_system(target_system)
        if not origin_id or not dest_id:
            return ""
        route = get_stargate_route(origin_id, dest_id)
        if route:
            jumps = len(route) - 1
            return f"{staging_system} -> {target_system}: **{jumps} jumps**"
    except Exception:
        pass
    return ""


def main():
    parser = argparse.ArgumentParser(description="FCTool Headless - zKill + Discord")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)

    # ── Discord setup ─────────────────────────────────────────────────────────
    dc = config.get("discord", {})
    if not dc.get("webhook_url"):
        print("[ERROR] No Discord webhook URL in config. Set discord.webhook_url")
        sys.exit(1)

    discord = DiscordNotifier(dc["webhook_url"])
    print(f"[{timestamp()}] Discord webhook configured")

    # ── zKillboard setup ──────────────────────────────────────────────────────
    zk = config.get("zkillboard", {})
    if not zk.get("enabled", True):
        print("[ERROR] zkillboard.enabled is false in config")
        sys.exit(1)

    alliance_names = zk.get("watch_alliances_names", {})
    region_names = zk.get("watch_regions_names", {})
    staging_system = zk.get("staging_system", "")

    alert_count = 0

    def on_alert(alert: KillAlert):
        nonlocal alert_count
        alert_count += 1

        # Get route from staging
        route_info = get_route_info(staging_system, alert.system_name)

        caps = " [CAPITALS]" if alert.capitals_involved else ""
        print(f"[{timestamp()}] ALERT #{alert_count}: "
              f"{alert.system_name} ({alert.region_name}){caps} "
              f"- {alert.kill_count} kills, {alert.total_value_millions:.0f}M ISK")
        if route_info:
            print(f"  Route: {route_info}")

        if dc.get("notify_zkill_alerts", True):
            discord.notify_zkill_alert(
                alert.system_name, alert.region_name,
                alert.kill_count, alert.pilots_on_field,
                alert.total_value_millions, alert.zkill_url,
                alert.capitals_involved,
                alert.dotlan_url,
                route_info,
                alert.capital_breakdown,
                alert.is_update,
                zkill_related_url=alert.zkill_related_url,
                warbeacon_url=alert.warbeacon_url,
                top_alliances=alert.top_alliances,
            )

    monitor = ZKillMonitor(
        watch_regions=zk.get("watch_regions", []),
        watch_alliances=zk.get("watch_alliances", []),
        watch_systems=zk.get("watch_systems", []),
        min_kill_value_millions=zk.get("min_kill_value_millions", 0),
        min_pilots_involved=zk.get("min_pilots_involved", 25),
        alert_window_seconds=zk.get("alert_window_seconds", 300),
        on_alert=on_alert,
    )

    # ── Startup ───────────────────────────────────────────────────────────────
    print(f"[{timestamp()}] FCTool Headless starting")
    print(f"  Regions: {len(zk.get('watch_regions', []))} "
          f"({', '.join(region_names.values()) if region_names else 'all'})")
    if alliance_names:
        print(f"  Alliances: {len(zk.get('watch_alliances', []))} "
              f"({', '.join(list(alliance_names.values())[:5])}...)")
    print(f"  Min pilots: {zk.get('min_pilots_involved', 25)} (any if capitals)")
    print(f"  Staging: {staging_system or 'not set'}")
    print(f"  Alert window: {zk.get('alert_window_seconds', 300)}s")
    print()

    monitor.start()

    # Send/create pinned status message (will be updated, not spammed)
    discord.send_or_update_status(online=True)

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    running = True

    def shutdown(sig, frame):
        nonlocal running
        print(f"\n[{timestamp()}] Shutting down (signal {sig})...")
        running = False
        monitor.stop()
        discord.send_or_update_status(online=False)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Main loop (just keep alive + heartbeat) ──────────────────────────────
    heartbeat_interval = 3600  # Log heartbeat every hour
    status_update_interval = 300  # Update Discord status every 5 min
    last_heartbeat = time.time()
    last_status_update = time.time()

    print(f"[{timestamp()}] Monitoring active. Press Ctrl+C to stop.")
    while running:
        try:
            time.sleep(10)
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                print(f"[{timestamp()}] Heartbeat - {alert_count} alerts sent this session")
                last_heartbeat = now
            # Periodically re-update status so timestamp stays fresh
            if now - last_status_update >= status_update_interval:
                discord.send_or_update_status(online=True)
                last_status_update = now
        except KeyboardInterrupt:
            shutdown(2, None)
            break

    print(f"[{timestamp()}] Stopped. {alert_count} alerts sent total.")


if __name__ == "__main__":
    main()
