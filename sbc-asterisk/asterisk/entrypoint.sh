#!/bin/sh
# Render Asterisk config templates from environment variables, then start
# Asterisk in the foreground.
set -eu

TEMPLATES=/etc/asterisk/templates
CONFIG=/etc/asterisk

# Expand ${VAR} placeholders in a template using the shell itself — the
# andrius/asterisk image does not ship envsubst (gettext). Templates are
# our own files, so letting the shell expand them is safe; values from
# .env are only ever substituted in, never evaluated.
render() {
  # Unset variables expand to empty (as envsubst did) rather than
  # tripping `set -u`.
  set +u
  eval "cat <<__BULLSEYE_TMPL__
$(cat "$1")
__BULLSEYE_TMPL__
"
  set -u
}

for tmpl in pjsip.conf ari.conf extensions.conf http.conf modules.conf; do
  if [ -f "$TEMPLATES/$tmpl.tmpl" ]; then
    render "$TEMPLATES/$tmpl.tmpl" > "$CONFIG/$tmpl"
  elif [ -f "$TEMPLATES/$tmpl" ]; then
    cp "$TEMPLATES/$tmpl" "$CONFIG/$tmpl"
  fi
done

# rtp.conf: lock down the RTP port range for firewall planning
cat > "$CONFIG/rtp.conf" <<'EOF'
[general]
rtpstart=10000
rtpend=10999
EOF

echo "Rendered config:"
ls -l "$CONFIG"/*.conf

exec asterisk -f -p -U asterisk -G asterisk
