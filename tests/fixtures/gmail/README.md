# Fixtures de alertas de Gmail

Estas fixtures son SINTETICAS y estan SIN VERIFICAR contra alertas reales.

No hay correos reales de LinkedIn ni de Indeed en este repo: se escribieron imitando lo
que se sabe publicamente del formato (maquetado con tablas, enlaces de rastreo, lineas de
ruido como «Easy Apply» o «Postulacion sencilla»). Si un test de `test_gmail_alertas.py`
pasa contra ellas, eso prueba que el parser hace lo que dice el codigo, no que lea bien
los correos de verdad.

Antes de prender `GEMA_GMAIL_ALERTAS` hay que correr `tools/autorizar_gmail.py --probar`
contra el buzon real. Cuando haya una alerta real, anonimizarla y agregarla aca como
fixture nueva: ese es el test que de verdad cierra la Fase 11.

- `linkedin_alerta_varios.html`  3 avisos, uno con salario, encabezado y pie
- `linkedin_alerta_uno.html`     1 aviso, en castellano, empresa y ubicacion en lineas aparte
- `indeed_alerta.html`           3 avisos + uno repetido (mismo `jk`) al final
- `sin_avisos.html`              correo de LinkedIn sin ningun aviso (notificacion de mensaje)
