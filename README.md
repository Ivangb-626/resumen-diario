# Resumen diario

Automatización gratuita que recopila hasta cinco noticias recientes relacionadas con tus intereses y las envía a tu Gmail cada día a las **08:00 (Europe/Berlin)** mediante GitHub Actions.

## Qué incluye

- Mercados, inversión a largo plazo y fondos indexados.
- Inteligencia artificial y tecnología.
- Ingeniería del software, desarrollo web y prácticas de 2027.
- IA aplicada a EEG y epilepsia.
- Actualidad de España y Alemania.
- Ajedrez y aprendizaje del ruso.
- Hasta cinco noticias, con titular, fuente, fecha, extracto RSS y enlace.

**Transparencia:** esta primera versión no usa una API de IA. Selecciona noticias y utiliza los extractos proporcionados por las fuentes RSS; no genera análisis editorial ni inventa explicaciones. No requiere claves de servicios de IA ni suscripciones adicionales.

## Configuración de Gmail (necesaria para el envío)

El repositorio no contiene contraseñas. Debes guardar dos secretos en GitHub:

1. Abre la configuración de secretos del repositorio:
   https://github.com/Ivangb-626/resumen-diario/settings/secrets/actions
2. Pulsa **New repository secret** y crea:
   - `GMAIL_ADDRESS`: tu dirección completa de Gmail.
   - `GMAIL_APP_PASSWORD`: una contraseña de aplicación de Google (16 caracteres), no tu contraseña normal de Gmail.

Google requiere tener activada la verificación en dos pasos para generar contraseñas de aplicación. Consulta la ayuda oficial:
https://support.google.com/accounts/answer/185833

Puedes crearla aquí:
https://myaccount.google.com/apppasswords

Ponle un nombre identificable, por ejemplo `GitHub resumen diario`. Guarda el código de 16 caracteres directamente en el secreto de GitHub. **No lo pegues en el chat ni lo guardes en un archivo del repositorio.** Si Google no te ofrece contraseñas de aplicación (por ejemplo, por políticas de una cuenta de organización o Protección Avanzada), no intentes usar tu contraseña habitual: habrá que cambiar a una integración OAuth.

El correo se envía a la misma dirección indicada en `GMAIL_ADDRESS`, desde `smtp.gmail.com` usando TLS.

## Probar y activar

Una vez añadidos ambos secretos:

1. Abre la pestaña **Actions**:
   https://github.com/Ivangb-626/resumen-diario/actions
2. Selecciona **Resumen diario**.
3. Pulsa **Run workflow** para hacer una prueba inmediata.
4. Comprueba que el trabajo termina en verde y que recibes el mensaje.

Después, el flujo se ejecutará automáticamente cada día a las 08:00, hora de Alemania. GitHub puede retrasar ocasionalmente los trabajos programados; no es un servicio de entrega con hora garantizada.

## Coste y privacidad

El repositorio es público. El código y los titulares registrados en los logs de ejecución pueden ser visibles públicamente; no se imprimen contraseñas ni secretos. GitHub indica que el uso de runners estándar alojados por GitHub es gratuito en repositorios públicos:
https://docs.github.com/en/actions/concepts/billing-and-usage

Google considera las contraseñas de aplicación un mecanismo menos seguro que «Iniciar sesión con Google». Utiliza una contraseña de aplicación específica y revócala en cuanto dejes de usar este flujo. No cambies la visibilidad del repositorio sin revisar antes las condiciones de facturación de Actions para repositorios privados.
