# Source this file after configuring HISTFILE, HISTSIZE, and SAVEHIST.
typeset -g _HISTFIX_HELPER="${${(%):-%x}:A:h}/histfix.py"

# Register completion without running compinit. A later compinit finds
# _histfix through fpath; after compinit, compdef registers it directly.
() {
  emulate -L zsh
  local dir=${_HISTFIX_HELPER:h}
  [[ -r $dir/_histfix ]] || return 0
  (( ${fpath[(Ie)$dir]} )) || fpath=("$dir" $fpath)
  if (( $+functions[compdef] )); then
    autoload -Uz _histfix
    compdef _histfix histfix
  fi
}

histfix() {
  local -i histfix_shared=0
  [[ -o share_history ]] && histfix_shared=1
  emulate -L zsh
  setopt extended_history
  local python="${HISTFIX_PYTHON:-python3}"
  local -i result=0
  # Validate before any history write. Help must also work without HISTFILE.
  command "$python" "$_HISTFIX_HELPER" --validate "$@" || result=$?
  (( result == 0 )) && return 0
  (( result == 11 || result == 12 )) || return $result

  if [[ ! -o interactive || -z $HISTFILE ]] || (( HISTSIZE <= 0 || SAVEHIST <= 0 )); then
    print -ru2 -- 'histfix: requires interactive zsh with HISTFILE, HISTSIZE, and SAVEHIST set'
    return 2
  fi

  # fc -AI rewrites its destination. Export in a subshell so a failed flush
  # neither rewrites existing records nor marks pending commands as saved.
  local -i histfix_histsize=$HISTSIZE histfix_savehist=$SAVEHIST
  local -i SAVEHIST=2147483647
  local histfix_tmp pending memory
  histfix_tmp=$(command mktemp -d "${TMPDIR:-/tmp}/histfix.XXXXXXXX") || return 2
  pending="$histfix_tmp/pending"
  memory="$histfix_tmp/memory"
  {
    ( builtin fc -AI "$pending" ) || return 2
    HISTFIX_FILE="$HISTFILE" command "$python" "$_HISTFIX_HELPER" --flush "$pending" || return 2
    # Mark the parent's events saved only after their durable append succeeds.
    builtin fc -AI "$pending" || return 2
    result=0
    if (( histfix_shared )); then
      # Other shells' imports change the event list at every prompt, so no
      # memory snapshot can be matched. fc -R cannot reset the shared-history
      # read position either. A new history level reads the rewritten file and
      # starts that position at its end, which avoids duplicate imports.
      HISTFIX_FILE="$HISTFILE" command "$python" "$_HISTFIX_HELPER" "$@" || result=$?
      (( result == 10 )) || return $result
      # fc -P later restores the values current at fc -p, so undo the
      # flush-only SAVEHIST override first.
      SAVEHIST=$histfix_savehist
      builtin fc -p "$HISTFILE" "$histfix_histsize" "$histfix_savehist" || return 2
      return 0
    fi
    # Inspect all events, including those excluded from the real history file.
    ( local HISTORY_IGNORE=''; builtin fc -W "$memory" ) || return 2
    HISTFIX_FILE="$HISTFILE" HISTFIX_MEMORY="$memory" HISTFIX_HISTORY_COUNT="${#history}" \
      command "$python" "$_HISTFIX_HELPER" "$@" || result=$?
    if (( result == 10 )); then
      local -i histfix_size=$HISTSIZE histfix_count=$(( ${#history} + 1 ))
      {
        HISTSIZE=0
        # The active event survives the clear. Cap the import so that this old
        # copy is evicted and the complete snapshot retains its original order.
        HISTSIZE=$histfix_count
        builtin fc -R "$memory" || return 2
      } always {
        HISTSIZE=$histfix_size
      }
      return 0
    fi
    return $result
  } always {
    command rm -f -- "$pending" "$memory"
    command rmdir -- "$histfix_tmp"
  }
}
