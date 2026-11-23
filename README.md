# Resumen diario

Automatización gratuita con GitHub Actions que recopila hasta cinco noticias recientes de tus intereses y las envía a tu correo cada día a las **08:00 (Europe/Berlin)**.

## Qué incluye

- Mercados, inversión a largo plazo y fondos indexados.
- Inteligencia artificial y tecnología.
- Ingeniería del software, desarrollo web y prácticas de 2027.
- IA aplicada a EEG y epilepsia.
- Actualidad de España y Alemania.
- Ajedrez y aprendizaje del ruso.
- Hasta cinco noticias con titular, fuente, fecha, extracto RSS y enlace.

**Transparencia:** esta versión no utiliza API de IA ni genera análisis editorial. Selecciona noticias recientes y presenta los extractos disponibles en los feeds RSS.

## Configurar Gmail SMTP

El repositorio nunca contiene contraseñas. Debes guardar los siguientes secretos en:
https://github.com/Ivangb-626/resumen-diario/settings/secrets/actions

1. `GMAIL_ADDRESS`: dirección de la cuenta Gmail que enviará el mensaje (remitente).
2. `GMAIL_APP_PASSWORD`: contraseña de aplicación de Google de 16 caracteres.
3. `GMAIL_RECIPIENT` (opcional): dirección que recibirá el correo. Si no se crea, se enviará a `GMAIL_ADDRESS`.

### Crear una contraseña de aplicación

Google exige que la verificación en dos pasos esté activada. Si no aparece la opción, Google indica que puede deberse a:
- Verificación en dos pasos configurada solo con llaves de seguridad.
- Cuenta de trabajo, universidad o gestionada por una organización.
- Protección Avanzada activada.

Ayuda oficial:
https://support.google.com/accounts/answer/185833

Página de contraseñas de aplicación:
https://myaccount.google.com/apppasswords

No desactives Protección Avanzada ni rebajes las opciones de seguridad de una cuenta administrada solo para habilitar SMTP. Si tu cuenta habitual no admite contraseñas de aplicación, una alternativa es usar otra cuenta Gmail personal elegible como remitente y fijar tu dirección habitual en `GMAIL_RECIPIENT`.

Genera una contraseña específica para este proyecto, no uses tu contraseña normal de Google. Guarda el código de 16 caracteres directamente como secreto `GMAIL_APP_PASSWORD`; no lo pegues en el chat ni lo subas al repositorio.

El envío se realiza por SMTP SSL a `smtp.gmail.com:465`. La dirección de remitente debe ser la cuenta que generó la contraseña de aplicación.

## Probar y activar

Una vez añadidos los secretos:
1. Abre https://github.com/Ivangb-626/resumen-diario/actions
2. Selecciona **Resumen diario**.
3. Pulsa **Run workflow** para una prueba inmediata.
4. Comprueba que el trabajo termina en verde y llega el correo.

Después, GitHub Actions intentará ejecutarlo cada día a las 08:00, hora de Alemania. GitHub puede retrasar ocasionalmente los trabajos programados y los repositorios públicos pueden tener los schedules desactivados tras 60 días sin actividad del repositorio.

## Coste y privacidad

El repositorio es público. El código y los titulares que aparezcan en los logs pueden ser visibles públicamente. Las contraseñas se guardan como secretos de GitHub y no se imprimen en los logs. No cambies la visibilidad del repositorio sin revisar antes las condiciones de facturación de Actions para repositorios privados.


## Pendientes personales

La lista inicial basada en tareas pendientes del contexto guardado de ChatGPT está en:
`data/pending_tasks.json`.

Se envía un segundo correo diario mediante el workflow **Pendientes por correo**, programado a las 08:15 (Europe/Berlin), usando los mismos secretos de Gmail que el resumen de noticias.

Los estados admitidos son:
- `pending`: pendiente.
- `in_progress`: en curso.
- `waiting`: esperando algo o a alguien.
- `completed`: completado; no aparece en el correo.

Para cerrar una tarea, cambia su estado a `completed`. Para añadir una tarea, agrega un nuevo objeto con un `id` único, `title`, `category`, `status`, `priority` y `next_action`.

**Nota:** esta lista es una carga inicial de lo que aparece pendiente en el contexto guardado. No existe sincronización automática entre la memoria de ChatGPT y GitHub; cuando me comuniques nuevos pendientes o tareas terminadas, actualizaré el JSON del repositorio.
