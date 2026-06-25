#!/bin/sh
# Render Asterisk config templates from environment variables, then start
# Asterisk in the foreground.
set -eu

TEMPLATES=/etc/asterisk/templates
CONFIG=/etc/asterisk

for tmpl in pjsip.conf ari.conf extensions.conf http.conf modules.conf; do
  if [ -f "$TEMPLATES/$tmpl.tmpl" ]; then
    envsubst < "$TEMPLATES/$tmpl.tmpl" > "$CONFIG/$tmpl"
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
