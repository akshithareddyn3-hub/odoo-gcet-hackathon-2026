const { PrismaClient } = require("@prisma/client");

// Reuse a single client instance across the app instead of creating
// a new one per request/module — avoids exhausting DB connections.
const prisma = new PrismaClient();

module.exports = prisma;
