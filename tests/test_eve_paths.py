"""
Tests for eve_paths — auto-detection of the EVE Online chat-logs folder.

Everything is injected/mocked: no test touches the real filesystem, registry,
or ctypes. ``documents``, ``home`` and ``exists`` are passed in directly, and
the few ``get_documents_dir`` tests stub ``sys.platform``/``os.path`` so the
Windows-only code paths are never exercised against the real machine.
"""

import os

import pytest

import eve_paths


# Normalising helper: build the expected EVE-logs path the same way the module
# does (os.path.join), so assertions are OS-separator agnostic.
def _logs(*parts):
    return os.path.join(*parts, "EVE", "logs", "Chatlogs")


# ── resolve_eve_logs_path: explicit setting respected ────────────────────────


def test_explicit_path_returned_unchanged_even_if_missing():
    """A real (non-placeholder) configured value is returned verbatim, even
    when the folder does not exist — an explicit user setting wins."""
    configured = "D:/Games/EVE/logs/Chatlogs"

    def exists(_):  # nothing exists
        return False

    out = eve_paths.resolve_eve_logs_path(
        configured,
        documents="C:/Users/bob/Documents",
        home="C:/Users/bob",
        exists=exists,
    )
    assert out == configured


def test_explicit_path_not_probed_for_existence():
    """When configured is explicit, candidate detection/exists is never run."""
    def boom(_):
        pytest.fail("exists() must not be called for an explicit setting")

    out = eve_paths.resolve_eve_logs_path(
        "E:/eve_logs",
        documents="C:/Users/bob/Documents",
        home="C:/Users/bob",
        exists=boom,
    )
    assert out == "E:/eve_logs"


# ── resolve_eve_logs_path: blank / placeholder trigger auto-detect ───────────


@pytest.mark.parametrize(
    "configured",
    [
        "",
        "   ",
        "C:/Users/YOUR_USER/Documents/EVE/logs/Chatlogs",  # shipped placeholder
        "c:/users/your_user/documents/eve/logs/chatlogs",  # case-insensitive
        None,  # missing entirely
    ],
)
def test_blank_or_placeholder_triggers_autodetect(configured):
    """Blank, whitespace, and any YOUR_USER placeholder auto-detect instead of
    being returned as-is."""
    home = "C:/Users/bob"
    documents = "C:/Users/bob/OneDrive/Documents"  # redirected Documents
    expected = _logs(documents)

    # Only the (documents-based) primary candidate exists.
    def exists(path):
        return path == expected

    out = eve_paths.resolve_eve_logs_path(
        configured, documents=documents, home=home, exists=exists
    )
    assert out == expected


# ── resolve_eve_logs_path: first existing candidate wins ─────────────────────


def test_autodetect_picks_first_existing_candidate_onedrive():
    """Inject exists so ONLY the OneDrive candidate exists -> it is chosen,
    even though the Documents-based candidate is earlier in the list."""
    home = "C:/Users/bob"
    documents = "C:/Users/bob/Documents"  # plain Documents (does NOT exist)
    # Build the OneDrive root with os.path.join so separators match production
    # on every OS (Windows uses backslashes under the join).
    onedrive_logs = _logs(os.path.join(home, "OneDrive", "Documents"))

    def exists(path):
        return path == onedrive_logs

    out = eve_paths.resolve_eve_logs_path(
        "", documents=documents, home=home, exists=exists
    )
    assert out == onedrive_logs


def test_autodetect_prefers_earlier_candidate_when_multiple_exist():
    """If several candidates exist, the highest-priority (documents) one wins."""
    home = "C:/Users/bob"
    documents = "C:/Users/bob/OneDrive/Documents"
    docs_logs = _logs(documents)

    def exists(_):  # everything exists
        return True

    out = eve_paths.resolve_eve_logs_path(
        "", documents=documents, home=home, exists=exists
    )
    assert out == docs_logs


# ── resolve_eve_logs_path: nothing exists -> primary default ─────────────────


def test_autodetect_no_candidate_exists_returns_primary_default():
    """When no candidate exists, the documents-based default is returned so the
    value is still sensible."""
    home = "C:/Users/bob"
    documents = "C:/Users/bob/Documents"
    expected = _logs(documents)

    def exists(_):
        return False

    out = eve_paths.resolve_eve_logs_path(
        "", documents=documents, home=home, exists=exists
    )
    assert out == expected


def test_autodetect_exists_raising_is_swallowed():
    """If the exists() predicate raises, detection still yields the default."""
    home = "C:/Users/bob"
    documents = "C:/Users/bob/Documents"
    expected = _logs(documents)

    def exists(_):
        raise OSError("permission denied")

    out = eve_paths.resolve_eve_logs_path(
        "", documents=documents, home=home, exists=exists
    )
    assert out == expected


# ── candidate_logs_paths ─────────────────────────────────────────────────────


def test_candidate_paths_include_documents_and_onedrive():
    home = "C:/Users/bob"
    documents = "C:/Users/bob/Documents"

    paths = eve_paths.candidate_logs_paths(documents=documents, home=home)

    assert _logs(documents) in paths
    assert _logs(os.path.join(home, "OneDrive", "Documents")) in paths
    # Documents-based candidate comes first.
    assert paths[0] == _logs(documents)


def test_candidate_paths_deduped_when_documents_equals_home_documents():
    """When the redirection-aware Documents equals <home>/Documents, the path
    appears once, not twice."""
    home = "C:/Users/bob"
    documents = os.path.join(home, "Documents")  # identical to home/Documents

    paths = eve_paths.candidate_logs_paths(documents=documents, home=home)

    assert paths.count(_logs(documents)) == 1


def test_candidate_paths_drop_blank_documents():
    """A falsy documents value contributes no candidate but home-based ones
    remain."""
    home = "C:/Users/bob"

    paths = eve_paths.candidate_logs_paths(documents="", home=home)

    # No None/blank leaked in.
    assert all(p for p in paths)
    # Home-based candidates still present.
    assert _logs(os.path.join(home, "Documents")) in paths
    assert _logs(os.path.join(home, "OneDrive", "Documents")) in paths


def test_candidate_paths_no_duplicates():
    home = "C:/Users/bob"
    documents = "C:/Users/bob/OneDrive/Documents"

    paths = eve_paths.candidate_logs_paths(documents=documents, home=home)

    assert len(paths) == len(set(paths))


def test_candidate_paths_discovers_onedrive_glob(monkeypatch):
    """OneDrive* directories (e.g. 'OneDrive - Contoso') are discovered via
    glob and contribute candidates. glob is stubbed so no real FS is touched."""
    home = "C:/Users/bob"
    documents = "C:/Users/bob/Documents"
    company_od = os.path.join(home, "OneDrive - Contoso")

    def fake_glob(pattern):
        # Sanity: we glob the OneDrive* pattern under home.
        assert pattern == os.path.join(home, "OneDrive*")
        return [company_od]

    monkeypatch.setattr(eve_paths.glob, "glob", fake_glob)

    paths = eve_paths.candidate_logs_paths(documents=documents, home=home)

    # Logs live under the OneDrive root's Documents subfolder.
    assert _logs(os.path.join(company_od, "Documents")) in paths


# ── Linux Wine/Proton/Lutris prefix discovery ────────────────────────────────


def test_candidate_paths_include_wine_prefix(tmp_path):
    """On Linux, EVE logs live inside a Wine prefix. Create a real fake prefix
    under tmp_path and assert its Chatlogs path is among the candidates.

    Uses real dirs (works on Windows too) rather than stubbing glob, so the
    actual filesystem globbing in candidate_logs_paths is exercised."""
    home = str(tmp_path)
    documents = os.path.join(home, "Documents")

    # ~/.wine/drive_c/users/alice/Documents/EVE/logs/Chatlogs
    wine_logs = _logs(
        os.path.join(home, ".wine", "drive_c", "users", "alice", "Documents")
    )
    os.makedirs(wine_logs)

    paths = eve_paths.candidate_logs_paths(documents=documents, home=home)

    assert wine_logs in paths


def test_resolve_picks_steam_proton_prefix_when_documents_missing(tmp_path):
    """resolve_eve_logs_path auto-detects a Steam-Proton compatdata prefix.

    The Documents-based candidate does not exist on disk, so resolution should
    fall through to the existing prefix Chatlogs path. Uses os.path.exists
    against real dirs created under tmp_path."""
    home = str(tmp_path)
    documents = os.path.join(home, "Documents")  # intentionally NOT created

    # ~/.local/share/Steam/steamapps/compatdata/8500/pfx/drive_c/users/
    #   steamuser/Documents/EVE/logs/Chatlogs
    proton_logs = _logs(
        os.path.join(
            home, ".local", "share", "Steam", "steamapps", "compatdata",
            "8500", "pfx", "drive_c", "users", "steamuser", "Documents",
        )
    )
    os.makedirs(proton_logs)

    out = eve_paths.resolve_eve_logs_path(
        "", home=home, documents=documents, exists=os.path.exists
    )
    assert out == proton_logs


# ── get_documents_dir (platform-stubbed, no real APIs) ───────────────────────


def test_get_documents_dir_non_windows_uses_home_documents(monkeypatch):
    """On non-Windows, get_documents_dir returns ~/Documents without touching
    ctypes/winreg."""
    monkeypatch.setattr(eve_paths.sys, "platform", "linux")
    monkeypatch.setattr(
        eve_paths.os.path, "expanduser", lambda p: "/home/bob"
    )

    out = eve_paths.get_documents_dir()
    assert out == os.path.join("/home/bob", "Documents")


def test_get_documents_dir_returns_none_when_everything_fails(monkeypatch):
    """If even the ~/Documents fallback raises, None is returned (never an
    exception)."""
    monkeypatch.setattr(eve_paths.sys, "platform", "linux")

    def boom(_):
        raise RuntimeError("no home")

    monkeypatch.setattr(eve_paths.os.path, "expanduser", boom)

    assert eve_paths.get_documents_dir() is None


def test_resolve_uses_get_documents_dir_when_documents_not_injected(monkeypatch):
    """When documents is not injected, candidate building falls back to
    get_documents_dir — stub it so no real OS call happens."""
    monkeypatch.setattr(
        eve_paths, "get_documents_dir", lambda: "C:/Users/bob/OneDrive/Documents"
    )
    home = "C:/Users/bob"
    expected = _logs("C:/Users/bob/OneDrive/Documents")

    def exists(path):
        return path == expected

    out = eve_paths.resolve_eve_logs_path("", home=home, exists=exists)
    assert out == expected
