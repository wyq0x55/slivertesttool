if (typeof window !== "undefined") {
  (window as any).global = (window as any).global || window;
  (window as any).process =
    (window as any).process || { env: { NODE_ENV: "production" } };
}
