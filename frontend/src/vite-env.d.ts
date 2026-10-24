/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  // BOOK-65: Payment's own service, until the gateway (BOOK-67)
  readonly VITE_PAYMENT_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
