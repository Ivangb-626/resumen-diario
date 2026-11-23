# Resumen diario

Proyecto gratuito para recopilar hasta cinco noticias recientes de tus intereses y enviarlas por correo cada mañana, incluso con el ordenador apagado.

## Cómo funciona

- **Google Apps Script** ejecuta el resumen diariamente en la nube, aproximadamente entre las 08:00 y las 09:00 (Europe/Berlin).
- **GitHub** conserva el código fuente.
- **GitHub Actions + clasp** permiten desplegar automáticamente en Apps Script cada cambio que se suba a `apps-script/`.
- El envío se realiza desde la cuenta Google que autorizó el proyecto.
- No se requiere una contraseña de aplicación de Gmail ni una API de IA de pago.

## Conectar este repositorio al proyecto de Apps Script (una sola vez)

Google ofrece CI/CD oficial con `clasp` y GitHub Actions. Sigue estos pasos una vez para autorizar la publicación automática.

### 1. Obtener el Script ID

1. Abre tu proyecto de Apps Script.
2. Entra en **Configuración del proyecto** (icono de engranaje).
3. En **IDs**, copia el **ID de secuencia de comandos (Script ID)**.

No es una contraseña, pero no lo publiques fuera de este proceso.

### 2. Habilitar la API de Apps Script

Abre https://script.google.com/home/usersettings y activa **Google Apps Script API**.

### 3. Obtener credenciales OAuth de clasp en tu ordenador

1. Asegúrate de tener Node.js 20 o posterior: https://nodejs.org/
2. Abre PowerShell.
3. Ejecuta:

   `npx --yes @google/clasp login`

4. Se abrirá el navegador. Accede con la misma cuenta Google propietaria del proyecto y concede el permiso para administrar Apps Script.
5. Al terminar, se habrá creado el archivo `%USERPROFILE%\.clasprc.json`.

**Ese archivo contiene un token de actualización que permite administrar tus proyectos de Apps Script. No lo pegues aquí, no lo subas al repositorio y no lo compartas con nadie.**

### 4. Añadir dos secretos al repositorio de GitHub

Abre https://github.com/Ivangb-626/resumen-diario/settings/secrets/actions y crea:

- `CLASPRC_JSON`: pega el contenido completo de `%USERPROFILE%\.clasprc.json`.
- `CLASP_JSON`: pega un JSON con el Script ID y la raíz del proyecto, con esta estructura:

   `{"scriptId":"PEGA_AQUI_EL_SCRIPT_ID","rootDir":"."}`

Sustituye el texto de ejemplo por el Script ID real. No añadas estos valores a un archivo del repositorio.

### 5. Desplegar y probar

1. Abre la pestaña Actions: https://github.com/Ivangb-626/resumen-diario/actions
2. Selecciona **Deploy Google Apps Script**.
3. Pulsa **Run workflow**.
4. Comprueba que termina en verde.

A partir de ahí, cuando ChatGPT modifique y guarde cualquier archivo dentro de `apps-script/` en la rama `main`, GitHub Actions subirá automáticamente el cambio al proyecto de Apps Script. No edites después el código manualmente en el editor de Apps Script, porque el repositorio será la fuente principal.

## Temas del resumen

- Mercados, inversión a largo plazo y fondos indexados.
- Inteligencia artificial y tecnología.
- Ingeniería del software, desarrollo web y prácticas de 2027.
- IA aplicada a EEG y epilepsia.
- Actualidad de España y Alemania.
- Ajedrez y aprendizaje del ruso.

El script selecciona titulares, extractos RSS, fuentes, fechas y enlaces. No llama a una API de IA y no inventa análisis.

## Horario y límites

Apps Script usa la zona `Europe/Berlin` y crea un disparador a las 08:00. Google puede ejecutar el disparador en cualquier momento dentro de la franja 08:00–09:00; no garantiza el minuto exacto.

Apps Script tiene cuotas gratuitas y límites sujetos a cambios. Revisa:
- https://developers.google.com/apps-script/guides/triggers/installable
- https://developers.google.com/apps-script/guides/services/quotas
- https://developers.google.com/apps-script/guides/clasp

## Seguridad

El repositorio es público. El código y los archivos de configuración que se confirmen pueden ser visibles; por eso las credenciales OAuth y el Script ID de despliegue se almacenan como secretos de GitHub. Nunca subas `.clasprc.json`, tokens ni contraseñas al repositorio.
