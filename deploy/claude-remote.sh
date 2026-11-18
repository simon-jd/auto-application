#!/bin/bash
# Claude Code Remote Control on home-ubuntu, as postedism: the user talks to Claude on this server from the Claude
# mobile app or claude.ai/code (Code → this machine). Each new conversation from the phone is a new session in the
# auto-application folder (--spawn same-dir), so the project's CLAUDE.md and skills apply as on the Mac, with
# AUTO_APP_HOME pointing at the runtime. Permission prompts that auto mode does not settle wait for the phone.
# Remote Control gives up after about 10 minutes without network and exits; the loop starts it again.
# Run by deploy/claude-remote.service inside tmux (`tmux -L claude-remote attach -t claude-remote` to see it). The first start asks
# "Enable Remote Control?" and to trust this folder: answer those once in the tmux session yourself.
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export AUTO_APP_HOME="$HOME/Desktop/job-search/runtime"
cd "$HOME/Desktop/job-search/auto-application" || exit 1
while true; do
  claude remote-control --spawn same-dir --permission-mode auto --name job-search
  code=$?
  printf '%s remote-control exited (%s); starting again in 30 s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$code"
  sleep 30
done
