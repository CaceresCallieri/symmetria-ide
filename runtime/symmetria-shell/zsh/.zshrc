# Symmetria IDE zsh interactive bootstrap.
#
# Sources the user's real ~/.zshrc first so their interactive setup is
# preserved (plugins, prompt, aliases, completions, custom hooks). Then
# installs the OSC 7 cwd emitter so the IDE's file tree follows shell
# `cd`. Phase 2.5 deliverable 3.
#
# Why ZDOTDIR (set by TerminalBackend.start() to this dir's parent):
# it's the standard pattern Kitty / Ghostty / WezTerm use for zsh
# shell-integration auto-injection. Setting ZDOTDIR redirects zsh's
# rcfile lookup; sourcing the user's real `~/.zshrc` from here gives
# them their full normal environment PLUS our OSC 7 hook.
#
# After we finish bootstrapping, we `unset ZDOTDIR` so any child shells
# the user spawns (nested zsh, scripts that re-exec) use the normal
# `~/.zshrc` lookup. Our injection is one-shot per IDE-spawned terminal.

# Restore normal lookup for any child shells.
unset ZDOTDIR

# Source the user's real interactive rcfile (Symmetria's ZDOTDIR
# redirect made zsh skip it).
[[ -r "$HOME/.zshrc" ]] && source "$HOME/.zshrc"

# OSC 7 emitter. Format: ESC ] 7 ; file://<host>/<path> ESC \
# (xterm spec — ST terminator, supported by every emulator that
# implements OSC 7; we use ST rather than BEL because some shells'
# zsh print builtins mangle BEL in certain quoting contexts).
symmetria_osc7() {
    printf '\e]7;file://%s%s\e\\' "${HOST:-${HOSTNAME:-localhost}}" "$PWD"
}

# Append to chpwd_functions so any user hooks already installed keep
# firing. Idempotent — even if the user re-sources this file, the
# duplicate function name is a no-op on the second pass.
if [[ -z "${chpwd_functions[(r)symmetria_osc7]:-}" ]]; then
    chpwd_functions+=(symmetria_osc7)
fi

# Fire once for the initial cwd so the IDE knows where the shell
# starts — the chpwd hook only fires on directory CHANGE, not on
# shell startup.
symmetria_osc7
