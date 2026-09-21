# Estándares de Codificación Corporativos

## Principios Generales

1. **Tipado Estricto**: Todo código nuevo debe usar tipado estático (TypeScript, Python con type hints, Go, Rust)
2. **Documentación**: Funciones públicas deben tener docstrings/comentarios JSDoc
3. **Testing**: Cobertura mínima 80% para código nuevo
4. **Linting**: Código debe pasar todos los linters configurados (ESLint, ruff, golangci-lint, clippy)

## Estilo de Código

### TypeScript/JavaScript
- Usar `const` por defecto, `let` solo cuando sea necesario
- Preferir `interface` sobre `type` para objetos
- Usar `unknown` en lugar de `any`
- Evitar `as any` y `@ts-ignore` - usar type guards
- Funciones flecha para callbacks, `function` para métodos de clase

### Python
- Usar `from __future__ import annotations`
- Type hints obligatorios en funciones públicas
- Usar `dataclasses` / `pydantic` para DTOs
- Evitar `Any` - usar `Protocol` o generics
- `ruff` como linter principal

### Go
- Seguir `gofumpt` para formato
- `golangci-lint` con configuración estricta
- Manejo de errores explícito - no `panic` en código de producción
- Usar `context.Context` en todas las operaciones I/O

### Rust
- `clippy` con `deny(warnings)`
- `rustfmt` estándar
- Preferir `Result<T, E>` sobre `Option<T>` para errores
- Usar `thiserror` para errores, `anyhow` solo en binarios

## Arquitectura

- **Separación de responsabilidades**: Capas claras (domain, application, infrastructure)
- **Inyección de dependencias**: Usar interfaces/ports, no implementaciones concretas
- **Inmutabilidad**: Preferir estructuras inmutables
- **Principio de responsabilidad única**: Un archivo = una responsabilidad

## Seguridad

- **Nunca** commitear secrets, API keys, tokens
- Usar variables de entorno para configuración sensible
- Validar y sanitizar toda entrada de usuario
- Usar prepared statements / ORM para queries SQL
- Implementar rate limiting en APIs públicas

## Git

- Commits atómicos y descriptivos (Conventional Commits)
- Branches: `feature/`, `fix/`, `chore/`, `docs/`, `refactor/`
- PRs requieren review y CI passing
- `main` protegido - solo merge via PR