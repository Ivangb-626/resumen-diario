# Resumen diario

Proyecto gratuito para recopilar hasta cinco noticias recientes de tus intereses y enviarlas por correo cada mañana, incluso con el ordenador apagado.

## Método recomendado: Google Apps Script

Esta alternativa evita las contraseñas de aplicación de Gmail, servidores externos, n8n y API de IA de pago. Google ejecuta el script en sus propios servidores después de que autorices el acceso una sola vez.

### Configuración inicial

1. Abre https://script.google.com/home/start con la cuenta de Google donde quieres recibir el resumen.
2. Crea un proyecto nuevo y llámalo **Resumen diario**.
3. Abre el archivo Code.gs, borra su contenido y pega el código de [apps-script/Code.gs](https://github.com/Ivangb-626/resumen-diario/blob/main/apps-script/Code.gs).
4. Guarda el proyecto.
5. En el selector de funciones, elige **setupDailyDigest** y pulsa **Ejecutar**.
6. Google te pedirá autorizar el envío de correos y las solicitudes a fuentes RSS. Acepta los permisos.
7. La función crea el disparador diario y envía un primer correo de prueba.

El script utiliza como destinataria la cuenta que creó y autorizó el proyecto.

### Horario

El disparador usa la zona **Europe/Berlin** y la hora 08:00. Google Apps Script puede ejecutar el disparador en cualquier momento entre las 08:00 y las 09:00; después mantiene esa hora aproximada día a día. No garantiza el minuto exacto.

### Temas incluidos

- Mercados, inversión a largo plazo y fondos indexados.
- Inteligencia artificial y tecnología.
- Ingeniería del software, desarrollo web y prácticas de 2027.
- IA aplicada a EEG y epilepsia.
- Actualidad de España y Alemania.
- Ajedrez y aprendizaje del ruso.

El script selecciona titulares, extractos RSS, fuentes, fechas y enlaces. No llama a una API de IA y no inventa análisis.

### Coste y límites

Apps Script tiene cuotas gratuitas sujetas a cambios. Para cuentas Gmail personales, la cuota publicada de MailApp es de 100 destinatarios por día, suficiente para un único correo diario. Cada ejecución tiene un límite de seis minutos.

Fuentes oficiales:
- https://developers.google.com/apps-script/guides/triggers/installable
- https://developers.google.com/apps-script/guides/services/quotas
- https://developers.google.com/apps-script/reference/mail/mail-app

### Gestionar el horario

Puedes ejecutar manualmente **sendDailyDigest** desde el editor para mandar otro resumen de prueba. Para borrar el horario, abre **Triggers / Desencadenadores** en el panel izquierdo y elimina el disparador de **sendDailyDigest**.
