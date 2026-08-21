import eslint from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: {
        document: "readonly",
        window: "readonly",
        fetch: "readonly",
        Headers: "readonly",
        RequestInit: "readonly",
        Response: "readonly",
        ResizeObserver: "readonly",
        crypto: "readonly",
      },
    },
  },
);
