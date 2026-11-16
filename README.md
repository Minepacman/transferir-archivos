# Transferir

Carpetas compartidas por el navegador y replicadas entre las computadoras de la red. Un solo archivo, sin dependencias (Python 3.8+).

## Qué hace
- **Compartida** y **Personal 1–3**: cada una con su PIN de 6 dígitos. Se replican entre todas las computadoras unidas
  (copia completa en `~/Transferencias/`, sincronización cada 10 s, borrados incluidos; gana el cambio más reciente).
- **PIN de equipo** (uno por computadora, no se replica): navegar Escritorio, Documentos y la carpeta de usuario de
  esa computadora desde el navegador (bajar y subir; no borra).
- Descubrimiento automático por difusión UDP (puerto 47800) y lista de nodos conocidos; mensajes entre nodos firmados
  con HMAC del secreto de la red. 5 PIN incorrectos → 1 minuto de espera.
- Arranque automático: LaunchAgent (macOS), unidad systemd de usuario (Linux) o carpeta Inicio (Windows).

## Uso
```bash
python3 transferir.py --instalar                       # primera computadora: crea la red y arranca sola
python3 transferir.py --info                           # direcciones, PINs y código de unión
python3 transferir.py --unirse http://IP:8000 --clave CODIGO   # otra computadora: se une, copia y arranca sola
python3 transferir.py --desinstalar                    # quita el arranque automático
```
Teléfonos: abrir `http://IP:8000` y escribir un PIN. Registro en `~/.transferir/registro.log`.

## SMB (explorador de archivos)
Además del navegador, las computadoras pueden abrir las carpetas por SMB con la cuenta del sistema:
- macOS: Ajustes → General → Compartir → Compartir archivos (y en Opciones, "Compartir archivos con Windows" para la
  cuenta). Agregar carpetas: `sudo sharing -a ~/Transferencias -S Transferencias`. Conectar: Finder → Ir → Conectarse
  al servidor → `smb://IP`.
- Windows: `\\IP` en el Explorador. Linux: `smb://IP` en el gestor de archivos.

Pensado para redes de confianza: HTTP sin cifrar.
