"""
FCTool - Fleet Commander Assistant for EVE Online

Main entry point that ties together:
- Chat log scanning with X-up counting
- zKillboard real-time engagement monitoring
- Discord webhook notifications
- Jump range calculations

Usage:
    python fc_tool.py                  # Run with config.json defaults
    python fc_tool.py --config my.json # Use custom config
    python fc_tool.py --range "C-N4OD" "1DQ1-A"  # Quick range check
"""

import argparse
import json
import os
import sys
import time
import threading
from datetime import datetime

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from chat_monitor import ChatMonitor, ChatMessage
from xup_counter import XUpCounter
from zkill_monitor import ZKillMonitor, KillAlert
from discord_notify import DiscordNotifier
from jump_range import JumpRangeChecker


# ANSI colors for terminal output
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    DIM = "\033[2m"


def timestamp():
    return datetime.now().strftime("%H:%M:%S")


class FCTool:
    def __init__(self, config_path: str = "config.json"):
        self.config = self._load_config(config_path)
        self.discord: DiscordNotifier | None = None
        self.chat_monitor: ChatMonitor | None = None
        self.xup_counter: XUpCounter | None = None
        self.zkill_monitor: ZKillMonitor | None = None
        self.jump_checker: JumpRangeChecker | None = None
        self._running = False

    def _load_config(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        print(f"{C.YELLOW}[Config] {path} not found, using defaults{C.RESET}")
        return {}

    def _setup_discord(self):
        dc = self.config.get("discord", {})
        if dc.get("enabled") and dc.get("webhook_url"):
            self.discord = DiscordNotifier(dc["webhook_url"])
            print(f"{C.GREEN}[Discord] Webhook configured{C.RESET}")
        else:
            print(f"{C.DIM}[Discord] Disabled (set discord.enabled=true and webhook_url in config){C.RESET}")

    def _setup_xup(self):
        xup_cfg = self.config.get("xup", {})

        def on_ready(state):
            msg = f"{C.GREEN}{C.BOLD}>>> FLEET READY! {state.count} x-ups (threshold: {xup_cfg.get('threshold', 30)}) <<<{C.RESET}"
            print(f"\n{msg}")
            print("\a")  # Terminal bell
            if self.discord and self.config.get("discord", {}).get("notify_xup_ready"):
                self.discord.notify_xup_ready(state.count, xup_cfg.get("threshold", 30))

        def on_fire(state):
            msg = f"{C.RED}{C.BOLD}>>> FIRE #{state.fire_count} CALLED - COUNTER RESET <<<{C.RESET}"
            print(f"\n{msg}")
            if self.discord and self.config.get("discord", {}).get("notify_xup_ready"):
                self.discord.notify_xup_fire(state.fire_count)

        def on_update(state):
            threshold = xup_cfg.get("threshold", 30)
            bar_len = 30
            filled = min(bar_len, int(bar_len * state.count / max(threshold, 1)))
            bar = "█" * filled + "░" * (bar_len - filled)
            color = C.GREEN if state.is_ready else C.YELLOW
            status = "READY!" if state.is_ready else "forming"
            sys.stdout.write(
                f"\r{C.CYAN}[X-Up]{C.RESET} {color}{bar} {state.count}/{threshold} [{status}]{C.RESET}    "
            )
            sys.stdout.flush()

        self.xup_counter = XUpCounter(
            trigger_word=xup_cfg.get("trigger_word", "x"),
            fire_word=xup_cfg.get("fire_word", "FIRE"),
            threshold=xup_cfg.get("threshold", 30),
            case_sensitive=xup_cfg.get("case_sensitive", False),
            on_ready=on_ready,
            on_fire=on_fire,
            on_update=on_update,
        )

    def _setup_chat_monitor(self):
        logs_path = self.config.get("eve_logs_path", "")
        if not logs_path or not os.path.isdir(logs_path):
            print(f"{C.RED}[Chat] Invalid logs path: {logs_path}{C.RESET}")
            print(f"{C.YELLOW}[Chat] Set 'eve_logs_path' in config.json{C.RESET}")
            return

        channel = self.config.get("xup", {}).get("channel_name", "Fleet")
        self.chat_monitor = ChatMonitor(
            logs_path=logs_path,
            poll_interval=self.config.get("poll_interval_seconds", 1.0),
            channel_filter=channel,
        )

        def on_message(msg: ChatMessage):
            if self.xup_counter:
                self.xup_counter.process_message(msg)

        self.chat_monitor.on_message(on_message)
        print(f"{C.GREEN}[Chat] Monitoring: {logs_path}{C.RESET}")
        print(f"{C.GREEN}[Chat] Channel filter: {channel}*{C.RESET}")

    def _setup_zkill(self):
        zk_cfg = self.config.get("zkillboard", {})
        if not zk_cfg.get("enabled"):
            print(f"{C.DIM}[zKill] Disabled (set zkillboard.enabled=true in config){C.RESET}")
            return

        def on_alert(alert: KillAlert):
            print(f"\n{C.RED}{C.BOLD}[FIGHT] {alert.system_name} ({alert.region_name}){C.RESET}")
            print(f"  Kills: {alert.kill_count} | Value: {alert.total_value_millions:.0f}M ISK")
            print(f"  {alert.zkill_url}")
            print("\a")
            if self.discord and self.config.get("discord", {}).get("notify_zkill_alerts"):
                self.discord.notify_zkill_alert(
                    alert.system_name, alert.region_name,
                    alert.kill_count, alert.pilots_on_field,
                    alert.total_value_millions, alert.zkill_url,
                    alert.capitals_involved,
                    alert.dotlan_url,
                    "",
                    alert.capital_breakdown,
                    alert.is_update,
                    zkill_related_url=alert.zkill_related_url,
                    warbeacon_url=alert.warbeacon_url,
                    top_alliances=alert.top_alliances,
                )

        self.zkill_monitor = ZKillMonitor(
            watch_regions=zk_cfg.get("watch_regions", []),
            watch_alliances=zk_cfg.get("watch_alliances", []),
            watch_systems=zk_cfg.get("watch_systems", []),
            min_kill_value_millions=zk_cfg.get("min_kill_value_millions", 0),
            min_pilots_involved=zk_cfg.get("min_pilots_involved", 10),
            alert_window_seconds=zk_cfg.get("alert_window_seconds", 300),
            on_alert=on_alert,
        )
        self.zkill_monitor.start()
        print(f"{C.GREEN}[zKill] Monitoring started{C.RESET}")
        if zk_cfg.get("watch_regions"):
            print(f"  Regions: {zk_cfg['watch_regions']}")
        if zk_cfg.get("watch_alliances"):
            print(f"  Alliances: {zk_cfg['watch_alliances']}")
        if not zk_cfg.get("watch_regions") and not zk_cfg.get("watch_alliances") and not zk_cfg.get("watch_systems"):
            print(f"  {C.YELLOW}(Watching ALL kills - set filters in config to narrow){C.RESET}")

    def _setup_jump_range(self):
        jr_cfg = self.config.get("jump_range", {})
        self.jump_checker = JumpRangeChecker(
            ship_type=jr_cfg.get("ship_type", "Dreadnought"),
            jdc_level=jr_cfg.get("jump_drive_calibration_level", 5),
            custom_ranges=jr_cfg.get("ranges_ly"),
        )

    def run(self):
        """Start all modules and run the main loop."""
        print(f"\n{C.BOLD}{C.CYAN}═══════════════════════════════════════{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  FCTool - Fleet Commander Assistant{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}═══════════════════════════════════════{C.RESET}\n")

        self._setup_discord()
        self._setup_xup()
        self._setup_chat_monitor()
        self._setup_zkill()
        self._setup_jump_range()

        print(f"\n{C.BOLD}Commands (type in terminal):{C.RESET}")
        print(f"  {C.CYAN}range <from> <to>{C.RESET}  - Check jump range between systems")
        print(f"  {C.CYAN}route <from> <to>{C.RESET}  - Get stargate route")
        print(f"  {C.CYAN}reset{C.RESET}              - Reset x-up counter")
        print(f"  {C.CYAN}status{C.RESET}             - Show current status")
        print(f"  {C.CYAN}quit{C.RESET}               - Exit")
        print()

        self._running = True

        # Start chat monitor polling in background thread
        if self.chat_monitor:
            chat_thread = threading.Thread(target=self._chat_poll_loop, daemon=True)
            chat_thread.start()

        # Main thread handles user input
        try:
            self._input_loop()
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}Shutting down...{C.RESET}")
        finally:
            self._running = False
            if self.zkill_monitor:
                self.zkill_monitor.stop()

    def _chat_poll_loop(self):
        while self._running:
            self.chat_monitor.poll()
            time.sleep(self.config.get("poll_interval_seconds", 1.0))

    def _input_loop(self):
        while self._running:
            try:
                user_input = input().strip()
            except EOFError:
                break

            if not user_input:
                continue

            parts = user_input.split()
            cmd = parts[0].lower()

            if cmd == "quit" or cmd == "exit":
                self._running = False
                break

            elif cmd == "reset":
                if self.xup_counter:
                    self.xup_counter.reset()
                    print(f"{C.YELLOW}[X-Up] Counter reset{C.RESET}")

            elif cmd == "status":
                self._print_status()

            elif cmd == "range" and len(parts) >= 3:
                origin = parts[1]
                dest = parts[2]
                self._do_range_check(origin, dest)

            elif cmd == "route" and len(parts) >= 3:
                origin = parts[1]
                dest = parts[2]
                self._do_route_check(origin, dest)

            else:
                print(f"{C.DIM}Unknown command: {user_input}{C.RESET}")
                print(f"{C.DIM}Commands: range, route, reset, status, quit{C.RESET}")

    def _print_status(self):
        print(f"\n{C.BOLD}── Status ──{C.RESET}")
        if self.xup_counter:
            s = self.xup_counter.state
            threshold = self.config.get("xup", {}).get("threshold", 30)
            status = f"{C.GREEN}READY{C.RESET}" if s.is_ready else f"{C.YELLOW}forming{C.RESET}"
            print(f"  X-Ups: {s.count}/{threshold} [{status}]")
            print(f"  Fires this session: {s.fire_count}")
            if s.xups:
                recent = sorted(s.xups.items(), key=lambda x: x[1], reverse=True)[:5]
                print(f"  Recent x-ups: {', '.join(name for name, _ in recent)}")
        if self.zkill_monitor:
            print(f"  zKill: {C.GREEN}Connected{C.RESET}")
        print()

    def _do_range_check(self, origin: str, dest: str):
        if not self.jump_checker:
            print(f"{C.RED}Jump checker not initialized{C.RESET}")
            return
        print(f"{C.DIM}Checking range {origin} -> {dest}...{C.RESET}")
        result = self.jump_checker.check_range(origin, dest)
        if "error" in result:
            print(f"{C.RED}{result['error']}{C.RESET}")
            return

        in_range = result["in_range"]
        color = C.GREEN if in_range else C.RED
        status = "IN RANGE" if in_range else "OUT OF RANGE"
        print(f"  {color}{C.BOLD}{status}{C.RESET}")
        print(f"  {result['origin']} -> {result['destination']}")
        print(f"  Distance: {result['distance_ly']} LY")
        print(f"  {result['ship_type']} range: {result['jump_range_ly']} LY")
        if result.get("gate_jumps") is not None:
            print(f"  Gate jumps: {result['gate_jumps']}")

    def _do_route_check(self, origin: str, dest: str):
        if not self.jump_checker:
            print(f"{C.RED}Jump checker not initialized{C.RESET}")
            return
        from jump_range import search_system, get_stargate_route, get_system_info
        print(f"{C.DIM}Calculating route {origin} -> {dest}...{C.RESET}")

        origin_id = search_system(origin)
        dest_id = search_system(dest)
        if not origin_id:
            print(f"{C.RED}System not found: {origin}{C.RESET}")
            return
        if not dest_id:
            print(f"{C.RED}System not found: {dest}{C.RESET}")
            return

        route = get_stargate_route(origin_id, dest_id)
        if not route:
            print(f"{C.RED}No route found{C.RESET}")
            return

        print(f"  Route ({len(route) - 1} jumps):")
        for sid in route:
            info = get_system_info(sid)
            name = info["name"] if info else str(sid)
            sec = info.get("security_status", 0) if info else 0
            sec_color = C.GREEN if sec >= 0.5 else (C.YELLOW if sec > 0.0 else C.RED)
            print(f"    {sec_color}{sec:+.1f}{C.RESET} {name}")


def main():
    parser = argparse.ArgumentParser(description="FCTool - EVE Online FC Assistant")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    parser.add_argument("--range", nargs=2, metavar=("FROM", "TO"),
                        help="Quick range check between two systems")
    parser.add_argument("--route", nargs=2, metavar=("FROM", "TO"),
                        help="Quick route check between two systems")
    args = parser.parse_args()

    # Handle quick commands
    if args.range:
        tool = FCTool(args.config)
        tool._setup_jump_range()
        tool._do_range_check(args.range[0], args.range[1])
        return

    if args.route:
        tool = FCTool(args.config)
        tool._setup_jump_range()
        tool._do_route_check(args.route[0], args.route[1])
        return

    # Full run
    tool = FCTool(args.config)
    tool.run()


if __name__ == "__main__":
    main()
