"""Fleet-chat "form" command -- the Tk-side controller (gates, apply, push, retry).

Design: ``docs/superpowers/specs/2026-11-22-fleet-chat-form-command-design.md``.
The pure grammar (parse + doctrine matching) is ``form_command.py``; this module
carries it out against the host (fc_gui's ``FCToolGUI``, duck-typed -- this
module never imports fc_gui) and fc_gui only wires it: it constructs one
``FormController(self)`` and calls ``observe(msg)`` from the chat hook.

Load-bearing invariants, each a place this feature could be silently wrong:

* **No modal dialogs.** Every refusal and failure is a toast over the client
  that typed the command (``refit_toast.RefitToast`` by default). Never a
  messagebox, a confirm or the composer's discard prompt -- a chat command
  arrives mid-fight.
* **No Tk off the Tk thread.** ``observe`` runs on the chat-poll thread and
  touches no Tk at all (not even a ``StringVar.get``): it reads the config dict,
  the parser, the live client list and its own cooldown, then marshals the
  query and the client rect through ``host._post_ui``. Everything else runs on
  the Tk thread, except the push worker, which talks to ESI only and marshals
  its outcome back through ``_post_ui``.
* **One doctrine end to end.** The doctrine switched, the doctrine reset and
  the doctrine serialised are the same one: ``apply_doctrine`` re-looks the
  doctrine up BY ID, refuses a name the composer could not select (it selects
  by name, first match), and ``push`` refuses (silently) when the composer no
  longer shows that id or has gained unsaved edits.
* **Unsaved composer edits are never discarded.** A dirty composer refuses the
  whole command and changes NOTHING.
* **Fleet and boss are re-asked at SEND time**, on the worker, for the
  composer's FC (``_motd_selected_fc_auth() or esi_auth``). A template can
  switch the FC without re-resolving the stored fleet, so the stored
  ``_motd_fleet_id`` may belong to another character: it is NEVER used, and
  "could not ask ESI" is a failure toast, not a fallback.
* **One ESI write per command.** ``push_inflight`` is True from before the
  refit reset (whose refresh chain would otherwise run the armed auto-update
  link's push) until ``_push_done`` or any early refusal; fc_gui's autopush
  returns early while it is set. A success is recorded as the link's baseline
  (``_motd_record_manual_push``). A retry whose markup the armed link already
  pushed in the meantime skips its PUT.
* **One live toast at a time**; a newer toast dismisses the older.
* **Retry is one click, one attempt.** The not-boss and unreachable toasts
  carry ``("retry", "Retry in 15 s")``; a pick schedules ONE ``root.after``.
  A newer ``apply_doctrine`` cancels a pending retry, and a fired retry goes
  through ``push`` and its composer checks, re-composing the markup.
* **No success toast** -- the MOTD itself is the confirmation; success shows on
  the MOTD tab's fleet-status label only.
"""
from __future__ import annotations

import threading

import form_command
import motd_builder
import range_check
import refit_command
import ui_theme
from app_log import get_logger

log = get_logger(__name__)

#: How long "Retry in 15 s" waits before re-running the push.
RETRY_DELAY_MS = 15_000

_DASH = chr(0x2014)
MSG_DIRTY = f"Unsaved MOTD edits {_DASH} not formed"
MSG_BUDGET = f"MOTD over budget {_DASH} not pushed"
MSG_NOT_BOSS = f"Not fleet boss {_DASH} MOTD not pushed"
MSG_UNREACHABLE = f"Could not reach ESI {_DASH} MOTD not pushed"
MSG_COMPOSE = f"Could not compose the MOTD {_DASH} not pushed"
MSG_NO_COMPOSER = f"MOTD composer not open {_DASH} not formed"
MSG_DUPLICATE = "Two doctrines are named '{name}' " + _DASH + " rename one"
MSG_NO_MATCH = "No doctrine matches '{text}'"
MSG_WHICH = "Which doctrine?"
STATUS_OK = "MOTD set successfully (204)."
STATUS_BUSY = "Setting MOTD..."
STATUS_FAILED = "Failed to set MOTD."
#: The fleet-status texts fc_gui's own fleet resolution writes.
_FLEET_BOSS = f"You are the fleet boss {_DASH} Set is enabled."
_FLEET_NOT_BOSS = (f"In a fleet but not the boss {_DASH} only the boss can "
                   "set the MOTD.")
_FLEET_NONE = "Not in a fleet."

#: The retry option row on the not-boss and unreachable toasts.
RETRY_OPTION = ("retry", "Retry in 15 s")
#: Every push outcome ``_push_done`` handles.
OUTCOMES = ("ok", "not_boss", "unreachable")


def _default_toast_factory(root, title, lines, options, on_pick=None,
                           on_dismiss=None):
    import refit_toast
    return refit_toast.RefitToast(
        root, title, lines, options,
        on_pick=on_pick if on_pick is not None else (lambda _oid: None),
        on_dismiss=on_dismiss)


def _default_thread_factory(target):
    threading.Thread(target=target, daemon=True,
                     name="form-motd-push").start()


def _title(name) -> str:
    return f"Form ({name})" if name else "Form"


class FormController:
    """Carries out ``form <doctrine>`` for one host. See the module docstring."""

    def __init__(self, host, *, toast_factory=None, thread_factory=None,
                 ok_colour=None):
        self.host = host
        self._toast_factory = toast_factory or _default_toast_factory
        self._thread_factory = thread_factory or _default_thread_factory
        # Looked up at construction, after the boot hook installed the palette
        # onto ui_theme -- the same value fc_gui's FG_GREEN import resolved to.
        self._ok_colour = ok_colour or ui_theme.FG_GREEN
        self._busy_colour = ui_theme.FG_ACCENT
        self._fail_colour = ui_theme.FG_RED
        self._cooldown = refit_command.Cooldown()
        self._foreign_senders: set = set()
        self._iconic_senders: set = set()
        self._toast = None
        self._retry_after_id = None
        self._push_gen = 0
        #: Generation of the push worker still out, or None.
        self._live_gen = None
        self._failed_push_ts = None
        #: True while a form push owns the MOTD (read by fc_gui's autopush).
        self.push_inflight = False

    # -- chat-poll thread -----------------------------------------------------

    def observe(self, msg) -> None:
        """Chat-tail hook: consider ONE fleet message as a form command.

        CHAT-POLL THREAD -- touches no Tk. Gates, cheapest first, in the order
        ``_refit_observe`` uses: master gate, parse, own live client (foreign
        sender = debug log once per name), not minimized (warning once per
        character), cooldown last so a refused line never burns it. Never
        raises."""
        try:
            host = self.host
            block = (getattr(host, "config", None) or {}).get("form_command")
            if not form_command.is_enabled(block):
                return
            keyword = form_command.normalize_config(block)["keyword"]
            query = form_command.parse(getattr(msg, "message", "") or "",
                                       keyword)
            if query is None:
                return
            key = range_check.own_key(getattr(msg, "sender", "") or "")
            clients = {c.key: c for c in host._range_check_clients(key)
                       if c.key}
            client = clients.get(key)
            if client is None:
                if key and key not in self._foreign_senders:
                    self._foreign_senders.add(key)
                    log.debug("[form] %s is not one of the owner's live "
                              "characters - command ignored", key)
                return
            if client.is_iconic:
                if key not in self._iconic_senders:
                    self._iconic_senders.add(key)
                    log.warning("[form] no on-screen EVE client for %s - "
                                "form command skipped", key)
                return
            if not self._cooldown.consider(key):
                return
            host._post_ui(self.resolve_and_apply, query, tuple(client.rect))
        except Exception:
            log.exception("[form] chat hook failed")

    # -- Tk thread ------------------------------------------------------------

    def resolve_and_apply(self, query, client_rect) -> None:
        """Tk thread: match the query and act on the verdict."""
        try:
            doctrines = list(self.host.fittings.list_doctrines() or ())
            res = form_command.match(doctrines, query)
        except Exception:
            log.exception("[form] resolving a chat command failed")
            return
        kind = res.kind
        if kind == "ignore":
            return
        if kind == "form":
            self.apply_doctrine(res.doctrine_id, client_rect)
            return
        if kind == "ambiguous":
            labels = dict(res.options)
            self._show_toast(
                "Form", [res.message or MSG_WHICH], res.options, client_rect,
                on_pick=lambda did: self.apply_doctrine(
                    did, client_rect, label=labels.get(did, "")))
            return
        self._show_toast("Form",
                         [res.message or MSG_NO_MATCH.format(text=query)],
                         (), client_rect)

    def apply_doctrine(self, doctrine_id, client_rect, label="") -> None:
        """Tk thread: select, load, reset, point the Fleet tab, push once.

        Cancels any pending retry first. Every refusal changes NOTHING."""
        self.cancel_retry()
        host = self.host
        try:
            doctrine = self._doctrine_by_id(doctrine_id)
            if doctrine is None:
                self._show_toast(
                    "Form",
                    [MSG_NO_MATCH.format(text=label or doctrine_id)], (),
                    client_rect)
                return
            name = str(getattr(doctrine, "name", "") or "")
            title = _title(name)
            var = getattr(host, "_motd_doctrine_var", None)
            if var is None:
                self._show_toast(title, [MSG_NO_COMPOSER], (), client_rect)
                return
            # The composer selects by NAME (first match): a doctrine shadowed
            # by an identically named one earlier in the library could never
            # be the one serialised.
            first = self._doctrine_by_name(name)
            if first is None or getattr(first, "id", None) != doctrine_id:
                self._show_toast(title, [MSG_DUPLICATE.format(name=name)], (),
                                 client_rect)
                return
            if host._motd_is_dirty():
                self._show_toast(title, [MSG_DIRTY], (), client_rect)
                return
        except Exception:
            log.exception("[form] checking doctrine %r failed", doctrine_id)
            return

        # Set BEFORE the reset: its refresh chain runs the armed link's push.
        self.push_inflight = True
        try:
            var.set(name)
            host._motd_doctrine_prev = name
            host._motd_on_doctrine_change()
            if not host._motd_apply_top_template():
                host._motd_load_default_doc()
            host._refits_reset(doctrine_id)
            host._auto_select_fleet_doctrine(name)
        except Exception:
            log.exception("[form] applying doctrine %r failed", doctrine_id)
            self._release()
            return
        self.push(doctrine_id, client_rect)

    def push(self, doctrine_id, client_rect, *, retry=False) -> bool:
        """Tk thread: compose and start ONE silent push worker.

        Drops silently (clearing ``push_inflight``) when the composer no longer
        shows ``doctrine_id`` or has unsaved edits. True iff a worker started."""
        host = self.host
        self.push_inflight = True
        title = "Form"
        try:
            shown = host._motd_selected_doctrine()
            if (shown is None or getattr(shown, "id", None) != doctrine_id
                    or host._motd_is_dirty()):
                log.info("[form] MOTD push for %r dropped - the composer "
                         "moved on", doctrine_id)
                self._release()
                return False
            title = _title(str(getattr(shown, "name", "") or ""))
            try:
                markup, _compacted = host._motd_output_markup()
            except Exception:
                log.exception("[form] composing the MOTD failed")
                self._release()
                self._show_toast(title, [MSG_COMPOSE], (), client_rect)
                return False
            if motd_builder.estimate_length(markup) >= host._motd_budget():
                self._release()
                self._show_toast(title, [MSG_BUDGET], (), client_rect)
                return False
            if retry and self._link_already_pushed(markup):
                log.info("[form] retry skipped - the auto-update link already "
                         "pushed this MOTD")
                self._release()
                return False
            auth = (host._motd_selected_fc_auth()
                    or getattr(host, "esi_auth", None))
            if auth is None or not getattr(auth, "is_authenticated", False):
                self._release()
                self._show_failure(title, "not_boss", doctrine_id,
                                   client_rect)
                return False
            host._motd_set_fleet_status(STATUS_BUSY, self._busy_colour)
            self._push_gen += 1
            gen = self._live_gen = self._push_gen
            try:
                self._thread_factory(
                    lambda: self._worker(auth, markup, doctrine_id,
                                         client_rect, gen))
            except Exception:
                self._live_gen = None          # no worker is out after all
                raise
            return True
        except Exception:
            log.exception("[form] starting the MOTD push failed")
            self._release()
            return False

    # -- worker ---------------------------------------------------------------

    def _worker(self, auth, markup, doctrine_id, client_rect, gen):
        """WORKER thread: ESI only. Marshals the outcome to ``_push_done``."""
        outcome, fleet_state = "unreachable", None
        try:
            outcome, fleet_state = self._send(auth, markup)
        except Exception as e:
            log.debug("[form] MOTD push failed: %s", e)
            outcome, fleet_state = "unreachable", None
        finally:
            self.host._post_ui(self._push_done, outcome, markup, doctrine_id,
                               client_rect, gen, fleet_state)

    @staticmethod
    def _send(auth, markup):
        """``(outcome, fleet_state)``; ``fleet_state`` = ``(fleet_id, is_boss)``
        when ESI answered the fleet question, else None. NEVER falls back to a
        stored fleet id."""
        getter = getattr(auth, "get_fleet_info_ex", None)
        if getter is None:
            return "unreachable", None
        result = getter()
        if not (isinstance(result, tuple) and len(result) == 2):
            return "unreachable", None
        info, status = result
        if status == 404:
            return "not_boss", (None, False)
        if status != 200 or not info:
            return "unreachable", None
        fleet_id = info.get("fleet_id")
        if not fleet_id:
            return "unreachable", None
        if not auth.is_boss(info, auth.character_id):
            return "not_boss", (fleet_id, False)
        put = auth.set_fleet_motd_ex(fleet_id, markup)
        if not (isinstance(put, tuple) and len(put) == 3):
            return "unreachable", None
        ok, put_status, _detail = put
        if ok:
            return "ok", (fleet_id, True)
        if put_status in (403, 404):
            return "not_boss", None
        return "unreachable", None

    # -- Tk thread: outcome ---------------------------------------------------

    def _push_done(self, outcome, markup, doctrine_id, client_rect, gen=None,
                   fleet_state=None):
        """Tk thread: clear ``push_inflight`` and report. A result from a push
        a newer one has superseded is dropped."""
        if gen is not None and gen != self._push_gen:
            log.info("[form] a superseded MOTD push finished (%s)", outcome)
            return
        self._live_gen = None
        self.push_inflight = False
        host = self.host
        try:
            self._note_fleet_state(fleet_state)
            if outcome == "ok":
                host._motd_record_manual_push(markup)
                host._motd_set_fleet_status(STATUS_OK, self._ok_colour)
                return
            if fleet_state is None:
                host._motd_set_fleet_status(STATUS_FAILED, self._fail_colour)
            doctrine = self._doctrine_by_id(doctrine_id)
            self._show_failure(_title(getattr(doctrine, "name", "") or ""),
                               outcome, doctrine_id, client_rect)
        except Exception:
            log.exception("[form] reporting the MOTD push failed")

    def _note_fleet_state(self, fleet_state):
        """Keep the composer's stored fleet / boss state (and its Set button)
        truthful for the FC ESI just answered for. Optional host seam."""
        apply = getattr(self.host, "_apply_motd_fleet_status", None)
        if apply is None or fleet_state is None:
            return
        fleet_id, is_boss = fleet_state
        if (getattr(self.host, "_motd_fleet_id", None) == fleet_id
                and bool(getattr(self.host, "_motd_is_boss", False))
                == bool(is_boss)):
            return
        if is_boss:
            text = _FLEET_BOSS
        elif fleet_id:
            text = _FLEET_NOT_BOSS
        else:
            text = _FLEET_NONE
        apply(fleet_id, is_boss, text)

    def _show_failure(self, title, outcome, doctrine_id, client_rect):
        self._failed_push_ts = getattr(self.host, "_motd_last_push_ts", None)
        text = MSG_NOT_BOSS if outcome == "not_boss" else MSG_UNREACHABLE
        self._show_toast(
            title, [text], (RETRY_OPTION,), client_rect,
            on_pick=lambda _oid: self.schedule_retry(doctrine_id,
                                                     client_rect))

    def _link_already_pushed(self, markup) -> bool:
        """Did the armed auto-update link push exactly ``markup`` since the
        push this retry is retrying failed?"""
        host = self.host
        return bool(
            getattr(host, "_motd_link_enabled", False)
            and markup == getattr(host, "_motd_last_pushed_markup", None)
            and getattr(host, "_motd_last_push_ts", None)
            != self._failed_push_ts)

    # -- retry ----------------------------------------------------------------

    def schedule_retry(self, doctrine_id, client_rect) -> None:
        """Tk thread: ONE push attempt ``RETRY_DELAY_MS`` from now."""
        self.cancel_retry()
        try:
            self._retry_after_id = self.host.root.after(
                RETRY_DELAY_MS,
                lambda: self._fire_retry(doctrine_id, client_rect))
        except Exception:
            log.exception("[form] scheduling the retry failed")
            self._retry_after_id = None

    def cancel_retry(self) -> None:
        aid, self._retry_after_id = self._retry_after_id, None
        if aid is None:
            return
        try:
            self.host.root.after_cancel(aid)
        except Exception:
            pass

    def _fire_retry(self, doctrine_id, client_rect):
        self._retry_after_id = None
        self.push(doctrine_id, client_rect, retry=True)

    # -- helpers --------------------------------------------------------------

    def _release(self):
        """An attempt ended without a worker: ``push_inflight`` stays True
        only while an earlier push's worker is still out."""
        self.push_inflight = self._live_gen is not None

    def _doctrines(self) -> list:
        try:
            return list(self.host.fittings.list_doctrines() or ())
        except Exception:
            log.exception("[form] listing doctrines failed")
            return []

    def _doctrine_by_id(self, doctrine_id):
        if not doctrine_id:
            return None
        for d in self._doctrines():
            if getattr(d, "id", None) == doctrine_id:
                return d
        return None

    def _doctrine_by_name(self, name):
        for d in self._doctrines():
            if (getattr(d, "name", "") or "") == name:
                return d
        return None

    def _show_toast(self, title, lines, options, client_rect, on_pick=None):
        """ONE live toast at a time: a newer one dismisses the older."""
        try:
            previous, self._toast = self._toast, None
            if previous is not None:
                previous.dismiss()
            holder = []

            def _gone():
                if holder and self._toast is holder[0]:
                    self._toast = None

            toast = self._toast_factory(
                self.host.root, title, [ln for ln in lines if ln],
                tuple(options or ()), on_pick=on_pick, on_dismiss=_gone)
            holder.append(toast)
            self._toast = toast
            toast.show(client_rect)
        except Exception:
            log.exception("[form] toast failed")
