# Transferir archivos

Servidor HTTP local para pasar archivos entre equipos de la misma red con el navegador. Un solo archivo, sin dependencias (Python 3.8+).

```bash
python3 transferir.py                 # comparte ~/Transferencias, puerto 8000, PIN al azar
python3 transferir.py --carpeta ~/Descargas --puerto 9000 --pin 1234
python3 transferir.py --sin-pin       # sin PIN (cualquiera en la red puede entrar)
```

Al arrancar imprime la dirección (`http://<ip>:8000`) y el PIN. En el otro equipo o teléfono: abrir la dirección,
escribir el PIN, arrastrar archivos para subirlos o tocar uno de la lista para bajarlo.

- Las subidas no sobrescriben: si el nombre existe se guarda como `nombre (1).ext`.
- Solo se aceptan nombres de archivo simples (sin carpetas ni `..`); se escribe a un `.parcial` y se renombra al terminar.
- Pensado para redes de confianza: va por HTTP sin cifrar.
