# Symmetria IDE zsh environment bootstrap.
#
# TerminalBackend.start() spawns the user's zsh with `ZDOTDIR` pointing
# at the parent directory of this file. With ZDOTDIR overridden, zsh
# reads `$ZDOTDIR/.zshenv` (this file) and `$ZDOTDIR/.zshrc` (sibling)
# INSTEAD of the user's `~/.zshenv` and `~/.zshrc`. We restore the
# user's environment by sourcing their real `~/.zshenv` here — anything
# the user's `.zshrc` depends on (PATH adjustments, env exports) lands
# before we hand off to `.zshrc`.

[[ -r "$HOME/.zshenv" ]] && source "$HOME/.zshenv"
