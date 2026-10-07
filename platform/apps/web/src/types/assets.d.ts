// Allow side-effect CSS imports in TypeScript tooling.
// (Next.js handles CSS at build time; this keeps `tsc --noEmit` happy.)
declare module '*.css';
