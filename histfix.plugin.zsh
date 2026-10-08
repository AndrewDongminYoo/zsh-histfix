# Source this file after configuring HISTFILE, HISTSIZE, and SAVEHIST.
typeset -g _HISTFIX_HELPER="${${(%):-%x}:A:h}/histfix.py"

histfix() {
  emulate -L zsh
  setopt extended_history
  local python="${HISTFIX_PYTHON:-python3}"
  local -i result=0
  # Validate before any history write. Help must also work without HISTFILE.
  command "$python" "$_HISTFIX_HELPER" --validate "$@" || result=$?
  (( result == 0 )) && return 0
  (( result == 11 )) || return $result

  if [[ ! -o interactive || -z $HISTFILE ]] || (( HISTSIZE <= 0 || SAVEHIST <= 0 )); then
    print -ru2 -- 'histfix: requires interactive zsh with HISTFILE, HISTSIZE, and SAVEHIST set'
    return 2
  fi

  # fc -AI rewrites its destination. Export in a subshell so a failed flush
  # neither rewrites existing records nor marks pending commands as saved.
  local -i SAVEHIST=2147483647
  local pending
  pending=$(command mktemp "${TMPDIR:-/tmp}/histfix.XXXXXXXX") || return 2
  {
    ( builtin fc -AI "$pending" ) || return 2
    HISTFIX_FILE="$HISTFILE" command "$python" "$_HISTFIX_HELPER" --flush "$pending" || return 2
    # Mark the parent's events saved only after their durable append succeeds.
    builtin fc -AI "$pending" || return 2
  } always {
    command rm -f -- "$pending"
  }
  local -i histfix_size=$HISTSIZE
  result=0
  HISTFIX_FILE="$HISTFILE" command "$python" "$_HISTFIX_HELPER" "$@" || result=$?
  if (( result == 10 )); then
    HISTSIZE=0
    HISTSIZE=$histfix_size
    builtin fc -R "$HISTFILE" || return 2
    return 0
  fi
  return $result
}
