#!/usr/bin/env bash
# Generate the bcrypt hash Caddy's basic_auth needs.
#
#   ./deployment/scripts/make-basicauth-hash.sh
#
# Prompts for the password without echoing it, prints the hash, and never
# prints, logs or stores the password itself. Put the hash in
# /etc/default/caddy; it is not a secret in the way the password is, but it is
# still a credential artefact and does not belong in the repository.
set -euo pipefail

if ! command -v caddy >/dev/null 2>&1; then
    echo "caddy is not on PATH. Install it first, or run this on the server." >&2
    exit 1
fi

printf 'Username: '
read -r username

# -s: no echo. The password never reaches the terminal, the scrollback, or
# the shell history -- which is also why it is not taken as an argument.
printf 'Password: '
read -rs password
printf '\n'

if [ -z "${password}" ]; then
    echo "Refusing to hash an empty password." >&2
    exit 1
fi

hash="$(printf '%s' "${password}" | caddy hash-password)"
unset password

cat <<OUT

Add these two lines to /etc/default/caddy (mode 0640, root:caddy):

CFD_BASIC_AUTH_USER=${username}
CFD_BASIC_AUTH_HASH=${hash}

Then:  sudo systemctl reload caddy
OUT
