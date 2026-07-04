"""Pure unit tests for agent_registry — the cross-IDE attribution fix.

Covers the on-disk routing registry (write/resolve/reap) and the pure
`resolve_slot_for_event` resolver that replaces trusting the daemon-frozen
`SYMMETRIA_AGENT_ID`. No Qt imports — mirrors test_agent_harness /
test_agent_activity.
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide import agent_registry


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Point the registry at a tmp dir and make every pid look alive by default."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(agent_registry, "_pid_alive", lambda pid: True)
    return tmp_path


def _mkrepo(base, name):
    """Create a fake repo root (a dir with a .git marker) and return its path."""
    root = base / name
    (root / ".git").mkdir(parents=True)
    return str(root)


# ---------------------------------------------------------------------------
# registry write / resolve_socket / reap
# ---------------------------------------------------------------------------


def test_write_and_resolve_by_session(registry_env):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {"sess-A": 1})
    # Exact session match wins regardless of cwd.
    assert (
        agent_registry.resolve_socket("sess-A", "/somewhere/else")
        == "/run/sock-700.sock"
    )


def test_resolve_by_project_root_when_session_unknown(registry_env):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {})
    # First event: session not yet bound anywhere → route by project root.
    sub = os.path.join(root, "src", "deep")
    os.makedirs(sub)
    assert agent_registry.resolve_socket("unbound-sess", sub) == "/run/sock-700.sock"


def test_resolve_prefers_session_over_root(registry_env):
    root_a = _mkrepo(registry_env, "a")
    root_b = _mkrepo(registry_env, "b")
    agent_registry.write_entry(700, "/run/sock-700.sock", root_a, {"sess-X": 2})
    agent_registry.write_entry(701, "/run/sock-701.sock", root_b, {})
    # session match on 700 beats a project-root match — even with a cwd in b.
    assert agent_registry.resolve_socket("sess-X", root_b) == "/run/sock-700.sock"


def test_resolve_returns_empty_when_no_match(registry_env):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {"sess-A": 1})
    assert agent_registry.resolve_socket("nope", "/tmp/unrelated") == ""


def test_resolve_skips_dead_pid_entries(registry_env, monkeypatch):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {"sess-A": 1})
    # 700 is dead → its entry must not capture events (stale file).
    monkeypatch.setattr(agent_registry, "_pid_alive", lambda pid: pid != 700)
    assert agent_registry.resolve_socket("sess-A", root) == ""


def test_remove_entry(registry_env):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {"sess-A": 1})
    agent_registry.remove_entry(700)
    assert agent_registry.resolve_socket("sess-A", root) == ""


def test_reap_dead_removes_dead_but_keeps_live(registry_env, monkeypatch):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {"a": 1})
    agent_registry.write_entry(701, "/run/sock-701.sock", root, {"b": 1})
    monkeypatch.setattr(agent_registry, "_pid_alive", lambda pid: pid == 701)
    agent_registry.reap_dead()
    files = {p.name for p in agent_registry.registry_dir().glob("*.json")}
    assert files == {"701.json"}


def test_read_tolerates_corrupt_entry(registry_env):
    root = _mkrepo(registry_env, "proj")
    agent_registry.write_entry(700, "/run/sock-700.sock", root, {"sess-A": 1})
    # A half-written / garbage sibling file must not break resolution.
    (agent_registry.registry_dir() / "999.json").write_text("{ not json")
    assert agent_registry.resolve_socket("sess-A", root) == "/run/sock-700.sock"


# ---------------------------------------------------------------------------
# resolve_slot_for_event — the poisoned-id-tolerant slot resolver
# ---------------------------------------------------------------------------


def _agents(**slots):
    """Build a {slot: inst} pool; each kwarg is slotN=(session_id, cwd, mono)."""
    out = {}
    for key, (sid, cwd, mono) in slots.items():
        out[int(key[4:])] = {"session_id": sid, "cwd": cwd, "spawn_mono": mono}
    return out


def test_slot_exact_session_match(tmp_path):
    root = _mkrepo(tmp_path, "proj")
    agents = _agents(slot1=("sess-A", root, 1.0))
    # Poisoned env id (another IDE's) is ignored; session id resolves it.
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-A", cwd=root, agent_id_env="999_3"
    )
    assert slot == 1
    assert bind == ""


def test_slot_legacy_env_match_binds_session(tmp_path):
    root = _mkrepo(tmp_path, "proj")
    agents = _agents(slot2=("", root, 1.0))
    # Un-poisoned in-process agent: env id is ours → slot 2, and we bind its
    # first-seen session id.
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-new", cwd=root, agent_id_env="700_2"
    )
    assert slot == 2
    assert bind == "sess-new"


def test_slot_legacy_env_match_rejected_for_foreign_cwd(tmp_path):
    """The tier-2 project guard: a daemon-FROZEN foreign event stamped with our
    pid prefix but reporting a DIFFERENT project's cwd must NOT steal our slot —
    it falls through (no unbound slot in the foreign root) to None."""
    root = _mkrepo(tmp_path, "proj")
    foreign = _mkrepo(tmp_path, "foreign")
    agents = _agents(slot2=("", root, 1.0))
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-frozen", cwd=foreign, agent_id_env="700_2"
    )
    assert slot is None
    assert bind == ""


def test_slot_legacy_env_match_accepted_when_cwd_absent(tmp_path):
    """The guard preserves pre-daemon behaviour: with no cwd to verify against,
    a matching env id is still trusted (can't be a routed foreign event, which
    always carries os.getcwd())."""
    root = _mkrepo(tmp_path, "proj")
    agents = _agents(slot2=("", root, 1.0))
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-new", cwd="", agent_id_env="700_2"
    )
    assert slot == 2
    assert bind == "sess-new"


def test_slot_project_root_claim_newest_unbound(tmp_path):
    root = _mkrepo(tmp_path, "proj")
    # Two unbound slots in the same project; the NEWEST spawn (higher mono) wins.
    agents = _agents(slot1=("", root, 1.0), slot3=("", root, 5.0))
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-Z", cwd=root, agent_id_env="999_9"
    )
    assert slot == 3
    assert bind == "sess-Z"


def test_slot_claim_matches_by_root_not_exact_cwd(tmp_path):
    root = _mkrepo(tmp_path, "proj")
    sub = os.path.join(root, "packages", "x")
    os.makedirs(sub)
    # Slot launched at repo root; event fires from a subdir (agent cd'd) — the
    # claim still matches because both resolve to the same project root.
    agents = _agents(slot1=("", root, 1.0))
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-Q", cwd=sub, agent_id_env="999_1"
    )
    assert slot == 1
    assert bind == "sess-Q"


def test_slot_no_match_returns_none(tmp_path):
    root = _mkrepo(tmp_path, "proj")
    other = _mkrepo(tmp_path, "other")
    # Poisoned env, unknown session, and no unbound slot in the event's project.
    agents = _agents(slot1=("sess-A", root, 1.0))
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-B", cwd=other, agent_id_env="999_1"
    )
    assert slot is None
    assert bind == ""


def test_slot_bound_slot_not_reclaimed_by_foreign_session(tmp_path):
    """A slot already bound to session A is NOT stolen by a different session's
    event in the same project when another unbound slot exists."""
    root = _mkrepo(tmp_path, "proj")
    agents = _agents(slot1=("sess-A", root, 1.0), slot2=("", root, 2.0))
    slot, bind = agent_registry.resolve_slot_for_event(
        agents, my_pid=700, session_id="sess-B", cwd=root, agent_id_env="999_9"
    )
    assert slot == 2  # the unbound one, not the bound slot 1
    assert bind == "sess-B"
