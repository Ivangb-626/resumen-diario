# Resumen diario

Briefing de actualidad en español, generado automáticamente con GitHub Actions y enviado por Gmail cada día a las **08:00 (Europe/Berlin)**.

## Contenido

El correo cubre las novedades publicadas durante las **últimas 24 horas**, sin un número máximo fijo de noticias. La selección es flexible: solo se incluyen noticias que aporten valor y no se rellenan secciones artificialmente.

1. **Economía y geopolítica** (prioridad principal): economía mundial, mercados, bancos centrales, empresas, conflictos, sanciones, energía, materias primas, bonos y divisas.
2. **España y Gipuzkoa**: política, economía, sociedad, País Vasco, Gipuzkoa y Donostia.
3. **Cultura, sociedad y descubrimientos**: historia, arqueología, patrimonio, libros, educación, medioambiente y descubrimientos.
4. **Ciencia, investigación y tecnología**: más peso para ciencia e investigación que para tecnología; neurociencia, cerebro, epilepsia, EEG, medicina, biología, genética, física, espacio e IA científica.

Todo el contenido editorial de las noticias (titulares, resúmenes y análisis) se redacta en castellano, aunque la fuente original publique en otro idioma. Las noticias se agrupan bajo cada sección con su número de noticias y se deduplican por URL/titular, incluidos titulares casi idénticos de distintos medios. El seguimiento de mercados aparece al principio del correo. No se incluyen imágenes en los correos para mantenerlos ligeros y centrados en el contenido. No se incluyen ofertas de empleo ni prácticas.

Al principio se añade un seguimiento indicativo de S&P 500, Nasdaq Composite, Euro Stoxx 50, Bitcoin, oro, petróleo WTI y EUR/USD, con la variación frente al cierre anterior disponible (sin Ibex 35). A continuación aparecen las cinco mayores subidas y las cinco mayores bajadas del día dentro de una cesta predefinida de grandes compañías internacionales conocidas, como NVIDIA, Apple, Microsoft, Amazon, Alphabet, Meta, Tesla, JPMorgan, ASML, SAP y LVMH. Así se evita que valores pequeños o poco conocidos dominen la lista por movimientos poco representativos.

## Cómo funciona

- Google News RSS aporta candidatos de distintas consultas en español e inglés.
- El script descarta noticias fuera de las últimas 24 horas y elimina duplicados.
- Gemini clasifica los candidatos por relevancia y redacta en castellano los titulares, resúmenes y análisis usando exclusivamente los titulares y extractos recibidos. No debe inventar datos ni enlaces.
- Los datos de mercado se consultan desde Yahoo Finance.

**Limitación importante:** el análisis de Gemini se basa en la información visible en los feeds RSS (titular y extracto), no garantiza que se haya leído el artículo completo. Los datos de mercado pueden tener retraso y no deben utilizarse como única fuente para invertir.

## Configuración de secretos

En [Settings → Secrets and variables → Actions](https://github.com/Ivangb-626/resumen-diario/settings/secrets/actions), configura:

| Secreto | Obligatorio | Uso |
|---|---|---|
| `GMAIL_ADDRESS` | Sí | Cuenta Gmail remitente |
| `GMAIL_APP_PASSWORD` | Sí | Contraseña de aplicación de Gmail |
| `GMAIL_RECIPIENT` | No | Destinatario; si falta, se usa GMAIL_ADDRESS |
| `GEMINI_API_KEY` | Recomendado | Clave de Gemini para selección y análisis editorial |

### Imágenes

Los correos se generan sin imágenes. No se realizan búsquedas, descargas ni adjuntos de fotografías.

### Obtener GEMINI_API_KEY
1. Abre [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Crea una API key para Gemini.
3. Añádela en GitHub como secreto llamado exactamente `GEMINI_API_KEY`.

El proyecto usa `gemini-3.1-flash-lite` por defecto. Los correos no incluyen imágenes. La disponibilidad y los límites de la modalidad gratuita dependen de la cuenta, el modelo y las cuotas vigentes. En la modalidad gratuita Google puede utilizar el contenido enviado para mejorar sus productos. En este proyecto se envían titulares y extractos públicos de noticias, no datos personales. Consulta [precios y condiciones oficiales](https://ai.google.dev/gemini-api/docs/pricing).

Si Gemini devuelve errores temporales de saturación (HTTP 429, 500, 502, 503 o 504), el script reintenta automáticamente hasta tres veces con esperas crecientes. Si finalmente no responde, el correo sigue enviándose con el seguimiento de mercados y un aviso del error, pero omite la sección de noticias: nunca publica titulares RSS sin revisión editorial. Cuando Gemini funciona, todos los titulares, resúmenes y análisis se generan en castellano.

### Gmail SMTP

El envío utiliza `smtp.gmail.com:465` mediante SSL. Crea una contraseña de aplicación específica y guárdala como `GMAIL_APP_PASSWORD`; no uses tu contraseña normal de Google ni la pegues en el repositorio.

Ayuda oficial: https://support.google.com/accounts/answer/185833

## Probar

1. Abre [GitHub Actions](https://github.com/Ivangb-626/resumen-diario/actions).
2. Selecciona **Resumen diario**.
3. Pulsa **Run workflow**.
4. Comprueba los logs y el correo recibido.

El workflow se ejecuta diariamente a las 08:00, hora de Alemania. GitHub puede retrasar ocasionalmente los trabajos programados.

## Privacidad y costes

- Las claves se almacenan en GitHub Actions Secrets, nunca en el código.
- GitHub Actions utiliza el runner alojado por GitHub.
- La API de Gemini tiene límites y condiciones de uso que pueden cambiar. La modalidad gratuita no equivale a una garantía de disponibilidad ilimitada ni de privacidad de nivel de pago.
- No se utiliza Google Search Grounding ni se activa facturación automáticamente desde este repositorio.
