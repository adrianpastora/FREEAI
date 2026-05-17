# Cómo publicar — chuleta personal

Dos acciones distintas. No mezclar.

| Quiero… | Comando |
|---|---|
| Subir código al repo público de GitHub | `git push origin main` |
| Desplegar al VPS | `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z` |

**Empujar a main NO despliega.** Solo los tags `v*` disparan `deploy-personal.yml`.

---

## Flujo de release al VPS

```bash
# 1. Asegúrate de que main está empujado primero.
git push origin main

# 2. Tag anotado con mensaje corto.
git tag -a v0.7.2 -m "v0.7.2 — qué cambia"

# 3. Empuja el tag. Esto dispara el deploy.
git push origin v0.7.2
```

Mira el run en https://github.com/adrianpastora/FREEAI/actions. Tarda 4–8 min.

---

## Re-disparar el deploy sin cambiar de versión

Si el deploy falló (secret roto, SSH caído) y quieres reintentar:

```bash
gh workflow run "Deploy FREEAI (maintainer-personal SSH+Docker)"
```

O botón **Run workflow** en la pestaña Actions.

---

## Qué número de versión usar

| Cambio desde el último tag | Bump |
|---|---|
| Solo bugfix, nada visible | patch — `v0.7.X+1` |
| Feature nueva, no rompe nada | minor — `v0.X+1.0` |
| Algo rompe (breaking) | minor en pre-1.0 + nota en CHANGELOG |

---

## Errores típicos a evitar

- Empujar `main` esperando que se despliegue — no pasa nada, hay que tagear.
- Tagear antes de empujar `main` — el VPS hace `git reset --hard origin/main`, así que el tag apuntaría a un commit que aún no está en el remoto y se desplegaría código viejo.
- Reusar un tag (`v0.7.0` ya existe) — no lo hagas, sube el número.
