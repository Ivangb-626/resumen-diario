# Resumen diario

Briefing de actualidad en español, generado automáticamente con GitHub Actions y enviado por Gmail cada día a las **08:00 (Europe/Berlin)**.

## Contenido

El correo cubre las novedades publicadas durante las **últimas 24 horas**, sin un número máximo fijo de noticias. La selección es flexible: solo se incluyen noticias que aporten valor y no se rellenan secciones artificialmente.

1. **Economía y geopolítica** (prioridad principal): economía mundial, mercados, bancos centrales, empresas, conflictos, sanciones, energía, materias primas, bonos y divisas.
2. **España y Gipuzkoa**: política, economía, sociedad, País Vasco, Gipuzkoa y Donostia.
3. **Cultura, sociedad y descubrimientos**: historia, arqueología, patrimonio, libros, educación, medioambiente y descubrimientos.
4. **Ciencia, investigación y tecnología**: más peso para ciencia e investigación que para tecnología; neurociencia, cerebro, epilepsia, EEG, medicina, biología, genética, física, espacio e IA científica.

Todo el contenido editorial de las noticias (titulares, resúmenes y análisis) se redacta en castellano, aunque la fuente original publique en otro idioma. Las noticias se agrupan bajo cada sección con su número de noticias. El seguimiento de mercados aparece al principio del correo. Cada noticia incluye una fotografía real incrustada siempre que alguna de las fuentes disponga de una imagen válida. Gemini convierte el titular y el resumen en consultas visuales; Commons y la página del medio se consultan en paralelo para poder comparar sus candidatas. Se ordenan por coincidencia de entidades y contexto, procedencia y metadatos, y las mejores se revisan visualmente con Gemini para descartar imágenes genéricas o ajenas a la noticia. Si Gemini no responde, se utiliza una puntuación local más conservadora, con un umbral superior para aceptar fotografías sin verificación visual. Después se prueban GDELT, Openverse y, opcionalmente, Brave. Cada descarga pasa controles de resolución, entropía y variedad de píxeles para descartar imágenes vacías o placeholders. Las fotografías meramente temáticas quedan reservadas al último recurso y se identifican como «imagen ilustrativa». No se incluyen ofertas de empleo ni prácticas.

Al principio se añade un seguimiento indicativo de S&P 500, Nasdaq Composite, Euro Stoxx 50, Bitcoin, oro, petróleo WTI y EUR/USD, con la variación frente al cierre anterior disponible (sin Ibex 35). A continuación aparecen las cinco mayores subidas y las cinco mayores bajadas del día dentro de una cesta predefinida de grandes compañías internacionales conocidas, como NVIDIA, Apple, Microsoft, Amazon, Alphabet, Meta, Tesla, JPMorgan, ASML, SAP y LVMH. Así se evita que valores pequeños o poco conocidos dominen la lista por movimientos poco representativos.

## Cómo funciona

- Google News RSS aporta candidatos de distintas consultas en español e inglés.
- El script descarta noticias fuera de las últimas 24 horas y elimina duplicados.
- Gemini clasifica los candidatos por relevancia y redacta en castellano los titulares, resúmenes y análisis usando exclusivamente los titulares y extractos recibidos. No debe inventar datos ni enlaces.
- Los datos de mercado se consultan desde Yahoo Finance.
- Gemini prepara dos consultas específicas por noticia. Commons y la página del medio (JSON-LD/Open Graph/HTML) se consultan en paralelo; si Google News solo ofrece una página intermedia o una portada de medio, se descartan sus imágenes de marca y no se confunden con fotos del artículo; las imágenes RSS también compiten como candidatas. Un ranking combina coincidencia con el titular, resumen y consultas, metadatos y procedencia. Pillow descarta imágenes vacías o de baja calidad. Cuando hay `GEMINI_API_KEY`, Gemini 3.1 Flash-Lite recibe hasta cuatro finalistas y clasifica su relación visual como directa, relacionada, ilustrativa o ajena; las dos primeras categorías pueden ganar la selección específica. Si el servicio falla, se utiliza el ranking local y se mantienen las fuentes de respaldo. Luego se prueban GDELT DOC 2.0, Openverse y, opcionalmente, Brave. Solo cuando se agotan las búsquedas específicas, Commons realiza varias consultas temáticas propias de cada noticia; no se reciclan paisajes urbanos genéricos para rellenar huecos. Las fotografías temáticas se etiquetan expresamente como ilustrativas y una noticia puede quedarse sin imagen si no hay ninguna que guarde relación. Se conservan los metadatos de autor/licencia cuando están disponibles.

**Limitación importante:** el análisis de Gemini se basa en la información visible en los feeds RSS (titular y extracto), no garantiza que se haya leído el artículo completo. Los datos de mercado pueden tener retraso y no deben utilizarse como única fuente para invertir.

## Configuración de secretos

En [Settings → Secrets and variables → Actions](https://github.com/Ivangb-626/resumen-diario/settings/secrets/actions), configura:

| Secreto | Obligatorio | Uso |
|---|---|---|
| `GMAIL_ADDRESS` | Sí | Cuenta Gmail remitente |
| `GMAIL_APP_PASSWORD` | Sí | Contraseña de aplicación de Gmail |
| `GMAIL_RECIPIENT` | No | Destinatario; si falta, se usa GMAIL_ADDRESS |
| `GEMINI_API_KEY` | Recomendado | Clave de Gemini para selección y análisis editorial |
| `BRAVE_SEARCH_API_KEY` | Opcional | Búsqueda adicional de imágenes cuando fallan las fuentes gratuitas anteriores |

### Obtener BRAVE_SEARCH_API_KEY (opcional)

1. Crea una suscripción de Brave Search API en [Brave Search API](https://brave.com/search/api/).
2. Genera una clave para la API de imágenes.
3. Añádela en GitHub Secrets con el nombre exacto `BRAVE_SEARCH_API_KEY`.

Si no se configura, el sistema sigue utilizando las imágenes del artículo, RSS y GDELT. Brave solo se consulta para noticias que continúan sin una imagen descargable. La búsqueda externa puede tener condiciones de uso y derechos de imagen propios; la similitud del titular se usa como filtro adicional, no como garantía de licencia.

### Obtener GEMINI_API_KEY

1. Abre [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Crea una API key para Gemini.
3. Añádela en GitHub como secreto llamado exactamente `GEMINI_API_KEY`.

El proyecto usa `gemini-3.1-flash-lite` por defecto. Además de generar consultas, el modelo puede revisar visualmente hasta cuatro fotografías finalistas por noticia. Las pruebas de regresión ejecutan 45 casos diez veces y una comprobación adicional con noticias RSS recientes; esta última publica una galería de imágenes como artefacto descargable de GitHub Actions durante siete días. La prueba mide también la proporción de fotografías específicas y no considera una imagen genérica reutilizada como cobertura satisfactoria. La disponibilidad y los límites de la modalidad gratuita dependen de la cuenta, el modelo y las cuotas vigentes. En la modalidad gratuita Google puede utilizar el contenido enviado para mejorar sus productos; en este proyecto se envían titulares y extractos públicos de noticias y, durante la selección visual, hasta cuatro fotografías públicas candidatas por noticia; no se envían datos personales. Consulta [precios y condiciones oficiales](https://ai.google.dev/gemini-api/docs/pricing).

Si Gemini devuelve errores temporales de saturación (HTTP 429, 500, 502, 503 o 504), el script reintenta automáticamente hasta tres veces con esperas crecientes. Si finalmente no responde, el correo sigue enviándose con el seguimiento de mercados y un aviso del error. Como respaldo, solo se muestran noticias RSS cuyo titular y extracto superan un filtro conservador de castellano; no se traducen ni se inventan contenidos. Las noticias que no superan ese filtro se omiten. Cuando Gemini funciona, todos los titulares, resúmenes y análisis se generan en castellano.

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
