/** @type {import('jest').Config} */
module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  // No `roots` override: jest validates every `roots` entry exists as a
  // directory, and test/ does not exist until group-2 (AGENT-51/52/53)
  // lands its suites. `testMatch` alone is a glob, which is fine to not
  // match anything -- paired with `test`'s `--passWithNoTests` flag, that
  // keeps `npm test` (and CI) green on this scaffold-only commit.
  testMatch: ["<rootDir>/test/**/*.test.ts"],
};
