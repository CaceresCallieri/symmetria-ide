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
# Note: if the login file above (commonly .bash_profile) already sources
# ~/.bashrc internally (a common distro pattern), then .bashrc will run
# twice per shell launch. Most .bashrc files are re-sourceable so this is
# acceptable for v1; add a $SYMMETRIA_BASHRC_SOURCED guard here if
# double-sourcing causes issues in practice.
[[ -r "$HOME/.bashrc" ]] && source "$HOME/.bashrc"

# OSC 7 emitter. Same wire format as the zsh variant: ESC ] 7 ;
# file://<host>/<path> ESC \ (ST terminator).
# HACK: $PWD is passed raw without percent-encoding — paths containing
# spaces, brackets, or other URI-reserved characters produce invalid
# file:// URIs per RFC 8089. Most terminals (Kitty, WezTerm, Ghostty)
# are lenient and accept unencoded paths today, but this is non-spec.
# PR 2's OSC 7 parser in TerminalBackend must handle unencoded paths.
# Remove once we add proper URL encoding (Python urllib.parse.quote or
# a POSIX-compatible shell encoder — sed-based encoding is fragile).
symmetria_osc7() {
    printf '\e]7;file://%s%s\e\\' "${HOSTNAME:-localhost}" "$PWD"
}

# Append to PROMPT_COMMAND so any user PROMPT_COMMAND keeps running.
# bash 5.0+ supports array PROMPT_COMMAND; we use the portable string
# form to avoid breaking on bash 4.x in unusual setups (Arch ships 5.x
# but the IDE's user-base may include macOS / older distros).
#
# The guard wraps PROMPT_COMMAND with sentinel semicolons and checks for
# ";symmetria_osc7;" (no spaces). The append must therefore also use NO
# space after the semicolon — using "; symmetria_osc7" (space) would
# make the guard fail to detect our hook on re-source, duplicating it.
if [[ -z "${PROMPT_COMMAND:-}" ]]; then
    PROMPT_COMMAND="symmetria_osc7"
elif [[ ";${PROMPT_COMMAND};" != *";symmetria_osc7;"* ]]; then
    # Idempotency guard — don't append twice if this script is sourced
    # multiple times (e.g. nested bash sessions).
    PROMPT_COMMAND="${PROMPT_COMMAND};symmetria_osc7"
fi

# Fire once for the initial cwd.
symmetria_osc7
