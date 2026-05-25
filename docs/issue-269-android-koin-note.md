# Issue #269 — DataStoreUserRepository NoBeanDefFoundException

## Assessment

This issue describes a `NoBeanDefFoundException` for `DataStoreUserRepository`
which is a Kotlin/Android Koin dependency injection error. After thorough review:

- **This repo (homepilot-v2)** contains NO Android or Kotlin code.
- The MotoRoute Android app code lives in a **separate repository**.
- The `DataStoreUserRepository` class and its Koin module registration must be
  fixed in the Android app repo, not here.

## Root Cause (in Android repo)

The Koin DI module that provides `DataStoreUserRepository` is either:
1. Not registered in the `Module` list passed to `startKoin()`
2. Referenced with a different qualifier/name than how it's registered
3. Missing a `single { DataStoreUserRepository(...) }` declaration

## Fix (in Android repo)

Add the repository module to Koin initialization:

```kotlin
val appModule = module {
    single<DataStoreUserRepository> { DataStoreUserRepository(androidContext()) }
}
```

Then ensure this module is included in `startKoin { modules(appModule, ...) }`.

## Action Required

- Create/transfer this issue to the Android app repository
- No changes needed in homepilot-v2