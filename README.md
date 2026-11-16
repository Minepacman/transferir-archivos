# transferir-archivos

Lo hice porque estaba harto de mandarme archivos por WhatsApp o andar con la USB para pasarlos entre mi Mac, la
compu de la casa y el teléfono. Es un servidor web chiquito que corre en cada computadora de la red; desde el
teléfono entras con el navegador, pones un PIN y subes o bajas lo que quieras. Y si tienes varias computadoras, las
carpetas se copian solas entre ellas.

Es un solo archivo de Python y no necesita instalar nada más (Python 3.8 o más nuevo). Lo he probado en macOS; en
Windows y Linux debería jalar igual, el arranque automático está hecho para los tres.

## Qué tiene

Hay cuatro carpetas que se sincronizan entre todas las computadoras: una **Compartida** y tres **personales**. Cada
una tiene su propio PIN, así que si le das a alguien el PIN de la compartida no puede ver las personales.

Aparte, cada computadora tiene un **PIN de equipo** que te deja navegar su Escritorio, Documentos y tu carpeta de
usuario desde el teléfono. Esas no se copian a las demás computadoras (serían demasiados gigas) y desde ahí no se
puede borrar nada, por si las dudas.

La página deja arrastrar archivos, enseña el progreso de la subida y se actualiza sola. En el teléfono se ve bien.

## Instalarlo

Necesitas Python 3:

- Mac: `brew install python` o el instalador de python.org
- Windows: el instalador de python.org (palomea "Add python.exe to PATH")
- Linux: casi siempre ya viene

En la primera computadora:

```bash
git clone https://github.com/Minepacman/transferir-archivos.git
cd transferir-archivos
python3 transferir.py --instalar
python3 transferir.py --info
```

`--instalar` lo deja corriendo y hace que arranque solo cada vez que prendes la compu. `--info` te dice la dirección
que tienes que abrir en el teléfono (algo como `http://192.168.1.50:8000`), los PINs y el código para unir otras
computadoras. En Windows usa `py` en vez de `python3`.

La primera vez la Mac pregunta si Python puede aceptar conexiones: dile que sí. En Windows el firewall pregunta lo
mismo; marca "redes privadas".

Si nada más lo quieres probar sin instalarlo: `python3 transferir.py` y lo cierras con Ctrl+C.

## Usarlo

Abre la dirección en el navegador del teléfono (tiene que estar en el mismo Wi-Fi), pon el PIN y listo. Para subir
tocas el recuadro o arrastras archivos; para bajar tocas el nombre. Si subes algo con un nombre que ya existe no lo
sobrescribe, lo guarda como `foto (1).jpg`.

Si se te olvidan los PINs: `python3 ~/.transferir/transferir.py --info`

## Agregar otra computadora

En la otra compu (con Python), baja `transferir.py` (está en la misma página, abajo, entrando con el PIN de equipo) y
corre:

```bash
python3 transferir.py --unirse http://192.168.1.50:8000 --clave EL_CODIGO
```

con la dirección y el código de unión que te dio `--info` en la primera. Se instala, se baja las carpetas y desde
ahí lo que cambies en una aparece en la otra en unos 10 segundos. Si borras algo también se borra en las demás.

## Cómo funciona

No hay servidor principal: todas las computadoras corren el mismo programa y son iguales.

- Cada 15 segundos cada una avisa por UDP a la red "aquí estoy" (puerto 47800), así se encuentran solas.
- Cada 10 segundos cada una le pregunta a las otras qué archivos tienen (nombre, tamaño y fecha) y se baja lo que
  le falte o lo que esté más nuevo. Si dos cambian el mismo archivo, gana el cambio más reciente.
- Cuando borras algo se guarda una "lápida" con la fecha, para que el archivo no regrese desde otra computadora que
  todavía lo tenía.
- Lo que se mandan entre computadoras va firmado con HMAC usando un secreto que solo conocen las que están unidas;
  si alguien de la red no lo tiene, no puede pedir ni meter archivos.

Los archivos sincronizados quedan en `~/Transferencias` y la configuración en `~/.transferir` (ahí están los PINs y
el secreto, no lo compartas).

## Por SMB (Samba)

Si prefieres abrir las carpetas en el Finder o el Explorador en vez del navegador, eso ya lo trae el sistema:

- En la Mac: Ajustes → General → Compartir → Compartir archivos. En las opciones activa "Compartir archivos con
  Windows" para tu usuario. Para que salga la carpeta de este programa:
  `sudo sharing -a ~/Transferencias -S Transferencias`
- En Windows: clic derecho en la carpeta → Propiedades → Compartir.
- En Linux: instalar `samba` y agregar la carpeta en `/etc/samba/smb.conf`.

Y te conectas con `smb://IP` (Mac/Linux) o `\\IP` (Windows) usando tu usuario y contraseña de esa computadora.

## Ojo

Va por HTTP normal, sin cifrar, así que úsalo en tu casa o en una red de confianza, no en el Wi-Fi de un café. El
PIN de equipo da acceso a toda tu carpeta de usuario, y el código de unión le da a la otra computadora una copia de
todas las carpetas (personales incluidas). Trátalos como contraseñas.

Lo que sí hace: PINs de 6 dígitos, bloqueo de un minuto después de 5 intentos mal, no deja salirse de la carpeta con
`../` ni con enlaces simbólicos, y no enseña archivos ocultos.

## Si algo falla

- **El teléfono no abre la página:** revisa que esté en el mismo Wi-Fi y el firewall de la computadora.
- **"Address already in use":** algo más usa el puerto 8000. Cámbialo con `python3 transferir.py --puerto 9000`.
- **No se ven entre computadoras:** algunos routers bloquean el UDP de difusión. No pasa nada, al unirlas con
  `--unirse` ya se conocen.
- **En la Mac dice que no hay permiso para Escritorio o Documentos:** Ajustes → Privacidad y seguridad → Acceso
  total al disco, y agrega el Python que dice el mensaje.
- **Algo no se sincroniza:** espera unos segundos y revisa `~/.transferir/registro.log`.

Para quitar el arranque automático: `python3 ~/.transferir/transferir.py --desinstalar` (no borra tus archivos).

## Para la presentación

Lo que pienso enseñar, con dos computadoras y un teléfono en el mismo Wi-Fi:

1. Instalar en la primera compu y sacar los PINs con `--info`.
2. Desde el teléfono, entrar con el PIN de la compartida y subir una foto.
3. Unir la segunda compu con `--unirse` y abrir su carpeta `Transferencias/Compartida`: la foto ya está ahí.
4. Copiar un archivo a mano en la segunda compu y ver que aparece en el teléfono. Luego borrarlo desde el teléfono y
   ver que desaparece en las dos.
5. Entrar con un PIN personal (solo se ve esa carpeta) y con el de equipo (Escritorio y Documentos).
6. Desconectar una compu del Wi-Fi, cambiar cosas en la otra y volverla a conectar para ver que se pone al día sola.

Lo que tiene de sistemas distribuidos: es peer-to-peer (sin servidor central), las computadoras se descubren solas,
la réplica es optimista con consistencia eventual, los conflictos se resuelven con "gana el último que escribió",
los borrados viajan como lápidas y los mensajes entre nodos van autenticados. Si una computadora se cae, las demás
siguen y cuando regresa se sincroniza.

Pendientes: HTTPS, guardar versiones en vez de que gane el último, y cifrar las carpetas personales.

— Elias Oziel Sánchez Fernández
