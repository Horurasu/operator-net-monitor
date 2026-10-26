#!/usr/bin/env bash
# Instala "Consumo de Red" en Linux: menú de aplicaciones, escritorio y
# registro automático en segundo plano al iniciar sesión.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/consumo-red"

# Dependencias
if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "Instalando tkinter..."
  if command -v apt >/dev/null; then sudo apt install -y python3-tk
  elif command -v pacman >/dev/null; then sudo pacman -S --noconfirm tk
  elif command -v dnf >/dev/null; then sudo dnf install -y python3-tkinter
  fi
fi
# vnstat es opcional: si está, la app también muestra el historial de antes de instalarla.

# tshark para registrar los sitios visitados, con permiso de captura para tu usuario
RELOGIN=0
if ! command -v tshark >/dev/null; then
  echo "Instalando tshark..."
  if command -v apt >/dev/null; then
    echo "wireshark-common wireshark-common/install-setuid boolean true" | sudo debconf-set-selections
    sudo DEBIAN_FRONTEND=noninteractive apt install -y tshark
  elif command -v pacman >/dev/null; then sudo pacman -S --noconfirm wireshark-cli
  elif command -v dnf >/dev/null; then sudo dnf install -y wireshark-cli
  fi
fi
if command -v apt >/dev/null && dpkg -s wireshark-common >/dev/null 2>&1; then
  echo "wireshark-common wireshark-common/install-setuid boolean true" | sudo debconf-set-selections
  sudo DEBIAN_FRONTEND=noninteractive dpkg-reconfigure wireshark-common >/dev/null 2>&1 || true
fi
getent group wireshark >/dev/null || sudo groupadd -r wireshark
if ! id -nG "$USER" | grep -qw wireshark; then
  sudo usermod -aG wireshark "$USER"
  RELOGIN=1
fi
DUMPCAP="$(command -v dumpcap || echo /usr/bin/dumpcap)"
if [ -x "$DUMPCAP" ] && [ "$(stat -c %G "$DUMPCAP")" != "wireshark" ]; then
  sudo chgrp wireshark "$DUMPCAP" && sudo chmod 750 "$DUMPCAP"
  sudo setcap cap_net_raw,cap_net_admin+eip "$DUMPCAP" || true
fi

mkdir -p "$DEST" "$HOME/.local/share/applications"
cp "$DIR/consumo_red.py" "$DEST/"
chmod +x "$DEST/consumo_red.py"

LANZADOR="[Desktop Entry]
Type=Application
Name=Consumo de Red
Comment=Consumo diario de red por cable y WiFi, y sitios visitados
Exec=python3 $DEST/consumo_red.py
Icon=network-wired
Terminal=false
Categories=Network;Utility;"

echo "$LANZADOR" > "$HOME/.local/share/applications/consumo-red.desktop"
chmod +x "$HOME/.local/share/applications/consumo-red.desktop"

ESCRITORIO="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$ESCRITORIO" ]; then
  echo "$LANZADOR" > "$ESCRITORIO/consumo-red.desktop"
  chmod +x "$ESCRITORIO/consumo-red.desktop"
  gio set "$ESCRITORIO/consumo-red.desktop" metadata::trusted true 2>/dev/null || true
fi

# Registro en segundo plano: arranca solo al iniciar sesión, y ahora mismo
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/consumo-red-registro.desktop" <<FIN
[Desktop Entry]
Type=Application
Name=Consumo de Red (registro)
Exec=python3 $DEST/consumo_red.py --registrar
NoDisplay=true
X-GNOME-Autostart-enabled=true
FIN
pkill -f "consumo_red.py --registrar" 2>/dev/null || true
sleep 1
nohup python3 "$DEST/consumo_red.py" --registrar >/dev/null 2>&1 &

echo "Listo. Búscala como \"Consumo de Red\" en tu menú de aplicaciones."
if [ "$RELOGIN" = 1 ]; then
  echo ""
  echo "IMPORTANTE: cierra sesión y vuelve a entrar (o reinicia) una vez,"
  echo "para que el registro de sitios tenga permiso de capturar."
fi
