'use strict';

module.exports = {
  env: {
    node: true,
    es2022: true,
    jest: true
  },
  extends: ['eslint:recommended'],
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'commonjs'
  },
  rules: {
    'no-console': 'off',
    'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    'no-var': 'error',
    'prefer-const': 'warn',
    'eqeqeq': ['error', 'always', { null: 'ignore' }],
    'no-eval': 'error',
    'no-implied-eval': 'error',
    'no-new-func': 'error',
    'no-prototype-builtins': 'off',
    'no-undef': 'error',
    'semi': ['error', 'always'],
    'no-extra-semi': 'error',
    'no-unreachable': 'error',
    'no-duplicate-case': 'error',
    'no-empty': ['warn', { allowEmptyCatch: true }],
    'no-shadow': 'off'
  },
  ignorePatterns: [
    'node_modules/',
    'public/',
    'coverage/',
    '*.min.js'
  ]
};
