# Flujo de Trabajo Git Corporativo

## Estructura de Branches

```
main                 # Protegida, solo merge via PR
  ├── develop        # Integración continua (opcional)
  ├── release/*      # Ramas de release (release/v1.2.0)
  ├── hotfix/*       # Fixes críticos en producción (hotfix/bug-description)
  ├── feature/*      # Nuevas funcionalidades (feature/JIRA-123-description)
  ├── fix/*          # Bug fixes (fix/JIRA-456-description)
  ├── chore/*        # Mantenimiento (chore/update-deps, chore/refactor-auth)
  └── docs/*         # Documentación (docs/api-update)
```

## Convención de Commits (Conventional Commits)

```
<type>[scope opcional]: <descripción corta>

[cuerpo opcional]

[footer opcional]
```

### Types
- `feat`: Nueva funcionalidad
- `fix`: Corrección de bug
- `docs`: Cambios en documentación
- `style`: Formato, punto y coma, etc (sin cambio de lógica)
- `refactor`: Refactorización sin cambio de funcionalidad
- `perf`: Mejora de rendimiento
- `test`: Añadir o corregir tests
- `chore`: Mantenimiento, build, deps, tooling
- `ci`: Cambios en CI/CD
- `security`: Cambios de seguridad

### Ejemplos
```
feat(auth): add OAuth2 PKCE support

fix(api): handle null response in user endpoint

chore(deps): update anthropic-sdk to v0.25.0

security(deps): patch CVE-2024-XXXX in lodash

refactor(core): extract validation logic to separate module
```

## Pull Requests

### Requisitos Mínimos
- [ ] Título sigue Conventional Commits
- [ ] Descripción clara del qué y por qué
- [ ] Tests pasando (unit + integration)
- [ ] Lint/Typecheck pasando
- [ ] Code review aprobado (mínimo 1 reviewer)
- [ ] No hay conflictos con `main`
- [ ] Commits squashados o rebasados limpiamente

### Template PR
```markdown
## Descripción
Breve explicación del cambio.

## Tipo de Cambio
- [ ] Bug fix
- [ ] Nueva feature
- [ ] Breaking change
- [ ] Docs update
- [ ] Refactor
- [ ] Chore

## Testing
- [ ] Unit tests añadidos/actualizados
- [ ] Integration tests pasando
- [ ] Probado manualmente en staging

## Checklist
- [ ] Código sigue style guide
- [ ] Self-review completado
- [ ] Comentarios en código complejo
- [ ] Documentación actualizada
- [ ] No hay warnings nuevos
```

## Code Review

### Como Autor
- PRs pequeños (< 400 líneas idealmente)
- Contexto suficiente en descripción
- Responder feedback en < 24h
- No tomar feedback personalmente

### Como Reviewer
- Revisar en < 24h
- Comentarios constructivos y específicos
- Aprobar si cumple estándares, aunque no sea "perfecto"
- `Request changes` solo si hay bugs o violaciones de seguridad/arquitectura

## Merge Strategy

- **Squash and merge** para feature/fix branches (historia limpia)
- **Rebase and merge** para hotfix/release (preservar commits atómicos)
- **No fast-forward** para mantener historia de merge
- Delete branch after merge (automático en GitHub/GitLab)

## Versionado (SemVer)

```
MAJOR.MINOR.PATCH
```

- `MAJOR`: Breaking changes incompatibles
- `MINOR`: Funcionalidad nueva compatible hacia atrás
- `PATCH`: Bug fixes compatibles hacia atrás

### Tags
```
v1.2.3          # Release
v1.2.3-rc.1     # Release candidate
v1.2.3-beta.2   # Beta
```

## Git Hooks (Husky/lefthook)

### pre-commit
- Lint (ESLint, ruff, golangci-lint, clippy)
- Format (prettier, gofumpt, rustfmt)
- Typecheck (tsc, mypy, go vet)
- Secrets scan (gitleaks, truffleHog)

### commit-msg
- Validar Conventional Commits
- Requerir issue reference (JIRA-XXXX)

### pre-push
- Tests rápidos (unit)
- Build verification

## Monorepo (si aplica)

- Carpeta por paquete/servicio: `packages/`, `services/`
- Shared tooling en root
- Changesets para versionado independiente
- Turbo/Nx para build cache y task orchestration