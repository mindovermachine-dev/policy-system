export default {
  theme: "mindovermachine-dev/mom-doc-theme@main",
  title: "policy-system",
  site: "https://docs.mindovermachine.dev",
  rootRedirect: null,
  defaultLocale: "root",
  locales: { root: { label: "English", lang: "en" } },
  astro: { base: "/policy-system", server: { host: true } },
  social: [
    {
      icon: "github",
      label: "GitHub",
      href: "https://github.com/mindovermachine-dev/policy-system",
    },
  ],
  sidebar: [
    {
      label: "Getting started",
      items: [{ autogenerate: { directory: "starting" } }],
    },
    {
      label: "Development",
      items: [{ autogenerate: { directory: "development" } }],
    },
  ],
};
