# Resumen diario

Proyecto gratuito para recopilar hasta cinco noticias recientes de tus intereses y enviarlas por correo cada mañana, incluso con el ordenador apagado.

## Cómo funciona

- **Google Apps Script** ejecuta el resumen diariamente en la nube, aproximadamente entre las 08:00 y las 09:00 (Europe/Berlin).
- **GitHub** conserva el código fuente.
- **GitHub Actions + clasp** despliegan automáticamente en Apps Script cada cambio que se suba a `apps-script/`.
- El envío se realiza desde la cuenta Google que autorizó el proyecto.
- No se requiere contraseña de aplicación de Gmail ni API de IA de pago.

## Conectar este repositorio al proyecto de Apps Script (una sola vez)

El repositorio ya incluye `apps-script/.clasp.json`, configurado con el Script ID que has compartido. El Script ID identifica el proyecto, pero no es una contraseña.

### 1. Habilitar la API de Apps Script

Abre https://script.google.com/home/usersettings y activa **Google Apps Script API**.

### 2. Autorizar clasp desde tu ordenador

Necesitamos generar una credencial OAuth para que GitHub Actions pueda publicar en tu proyecto. Se hace una sola vez:

1. Abre PowerShell en Windows.
2. Comprueba si Node.js está instalado con `node --version`. Si no lo está, instálalo desde https://nodejs.org/ (versión LTS).
3. Ejecuta:

   `npx --yes @google/clasp login`

4. Se abrirá el navegador. Accede con la misma cuenta Google propietaria del proyecto y concede el acceso solicitado.
5. Cuando finalice, clasp habrá creado `%USERPROFILE%\.clasprc.json`.

**Ese archivo contiene una credencial OAuth privada. No lo pegues en el chat, no lo subas al repositorio ni lo compartas con nadie.**

### 3. Guardar la credencial como secreto de GitHub

1. Abre https://github.com/Ivangb-626/resumen-diario/settings/secrets/actions
2. Pulsa **New repository secret**.
3. En **Name**, escribe exactamente `CLASPRC_JSON`.
4. En **Secret**, pega el contenido completo del archivo `%USERPROFILE%\.clasprc.json`.
   - Puedes abrirlo en el Bloc de notas con `notepad $HOME\.clasprc.json`.
   - Copia todo el contenido JSON y pégalo directamente en el campo Secret de GitHub.
   - No envíes ese contenido por ChatGPT.
5. Guarda con **Add secret**.

No necesitas crear el secreto `CLASP_JSON`: la configuración de proyecto ya está incluida en el repositorio.

### 4. Ejecutar el primer despliegue

1. Abre la pestaña Actions: https://github.com/Ivangb-626/resumen-diario/actions
2. Selecciona **Deploy Google Apps Script**.
3. Pulsa **Run workflow**.
4. Comprueba que termina en verde.

A partir de ahí, cuando ChatGPT modifique y guarde cualquier archivo dentro de `apps-script/` en la rama `main`, GitHub Actions subirá automáticamente los cambios al proyecto de Apps Script. No edites después el código manualmente en el editor de Apps Script: el repositorio será la fuente principal.

**Importante:** `clasp push --force` reemplaza el contenido del proyecto remoto con los archivos del repositorio. Antes del primer despliegue, asegúrate de que el código que ya ejecutaste en Apps Script está representado en `apps-script/Code.gs` y `apps-script/appsscript.json`. El repositorio debe ser la fuente de verdad a partir de ese momento.

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

El repositorio es público. El código y el archivo `.clasp.json` (que contiene el Script ID, no credenciales) pueden ser visibles. La credencial OAuth se guarda únicamente en el secreto `CLASPRC_JSON`. Nunca subas `.clasprc.json`, tokens ni contraseñas al repositorio.
