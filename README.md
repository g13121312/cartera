# Dashboard de cartera con NAV diarios

Qué hace: cada día laborable un job de GitHub Actions descarga el NAV de tus fondos, lo guarda en `data/navs.json`, y `index.html` dibuja la cartera (base 100) en 1S, 1M, 3M, 6M, YTD, 1A, 3A y Máx, con rentabilidad, volatilidad, caída máxima y aportación de cada fondo.

## Puesta en marcha (10 minutos)

1. **ISIN.** Abre `funds.json` y rellena el `isin` exacto de la clase que tienes de cada fondo (EUR, acumulación). Ajusta los `weight` (se normalizan a 100 solos).
2. **Repositorio.** Crea un repo en GitHub y sube todo el contenido de esta carpeta, incluida `.github/workflows/update.yml`.
3. **Permisos.** En *Settings → Actions → General → Workflow permissions*, marca *Read and write permissions*.
4. **Primera carga.** En la pestaña *Actions*, abre «Actualizar NAV» y pulsa *Run workflow*. Revisa el log: te dice qué fondos se actualizaron y cuáles fallaron.
5. **Publicar.** En *Settings → Pages*, elige la rama `main` y la carpeta raíz. Tu dashboard queda en `https://<usuario>.github.io/<repo>/`.

Para verlo en local: `python update_navs.py` y luego `python -m http.server`, y abre `http://localhost:8000`.

## Si un fondo no aparece

- Yahoo Finance no cubre todos los fondos. Prueba a poner su `ticker` de Yahoo en `funds.json`.
- Si no está en Yahoo, crea `data/manual/<ISIN>.csv` con líneas `2026-09-19,12.345` (fecha ISO, punto decimal). Se mezcla con lo descargado y lo manual manda.
- Comprueba que la clase sea en EUR: si Yahoo devuelve la clase en USD, la rentabilidad no será la de tu posición.

## Privacidad

En un repo público, `funds.json` (fondos y pesos) es visible para cualquiera. Si no quieres eso, usa repo privado (GitHub Pages en privado requiere plan de pago) o pon pesos ficticios que sumen lo mismo.

## Cómo se calcula

La cartera se rebasa a 100 al inicio de cada periodo con los pesos de `funds.json` (equivale a rebalancear a pesos objetivo en esa fecha). Si un fondo tiene menos historia que el periodo, la ventana arranca cuando todos tienen datos y la fecha real de inicio aparece bajo la cifra grande. Es rentabilidad de la cartera modelo, no incluye tus aportaciones reales ni comisiones de traspaso.
