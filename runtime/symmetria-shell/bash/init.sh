# Symmetria IDE bash bootstrap (sourced via `bash --rcfile <this>`).
#
# Unlike zsh's ZDOTDIR which redirects all rcfile lookup, bash's
# `--rcfile` only applies to interactive non-login shells — `-l` would
# make bash ignore --rcfile entirely. TerminalBackend.start() therefore
# spawns bash WITH `-i` and `--rcfile=<this>` (no `-l`), and this
# script sources the login + interactive files manually so the user's
# full environment is preserved.
#
# Phase 2.5 deliverable 3 — terminal-driven cwd → file tree sync via
# OSC 7.

# Source login-shell files first — first existing one wins, matching
# bash's normal lookup precedence (.bash_profile → .bash_login → .profile).
for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    if [[ -r "$f" ]]; then
        source "$f"
        break
    fi
done

# Source interactive rcfile.
[[ -r "$HOME/.bashrc" ]] && source "$HOME/.bashrc"

# OSC 7 emitter. Same wire format as the zsh variant: ESC ] 7 ;
# file://<host>/<path> ESC \ (ST terminator).
symmetria_osc7() {
    printf '\e]7;file://%s%s\e\\' "${HOSTNAME:-localhost}" "$PWD"
}

# Append to PROMPT_COMMAND so any user PROMPT_COMMAND keeps running.
# bash 5.0+ supports array PROMPT_COMMAND; we use the portable string
# form to avoid breaking on bash 4.x in unusual setups (Arch ships 5.x
# but the IDE's user-base may include macOS / older distros).
if [[ -z "${PROMPT_COMMAND:-}" ]]; then
    PROMPT_COMMAND="symmetria_osc7"
elif [[ ";${PROMPT_COMMAND};" != *";symmetria_osc7;"* ]]; then
    # Idempotency guard — don't append twice if this script is sourced
    # multiple times (e.g. nested bash sessions).
    PROMPT_COMMAND="${PROMPT_COMMAND}; symmetria_osc7"
fi

# Fire once for the initial cwd.
symmetria_osc7
