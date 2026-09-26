require("dotenv").config();
const express = require("express");
const cors = require("cors");
const prisma = require("./lib/prisma");

const app = express();
app.use(cors());
app.use(express.json());

// Health check — also confirms the DB connection works, since that's the
// most common thing to break right before a demo.
app.get("/api/health", async (req, res) => {
  try {
    await prisma.$queryRaw`SELECT 1`;
    res.json({ status: "ok", db: "connected" });
  } catch (err) {
    res.status(500).json({ status: "error", db: "unreachable", error: err.message });
  }
});

// --- Feature routes go here as each branch merges in ---
// app.use("/api/auth", require("./routes/auth"));
// app.use("/api/products", require("./routes/products"));
// app.use("/api/warehouses", require("./routes/warehouses"));
// app.use("/api/receipts", require("./routes/receipts"));
// app.use("/api/deliveries", require("./routes/deliveries"));
// app.use("/api/transfers", require("./routes/transfers"));
// app.use("/api/adjustments", require("./routes/adjustments"));
// app.use("/api/dashboard", require("./routes/dashboard"));

// Centralized error handler — keeps validation errors consistent
// across every route instead of each one formatting errors differently.
app.use((err, req, res, next) => {
  console.error(err);
  res.status(err.status || 500).json({
    error: err.message || "Internal server error",
  });
});

const PORT = process.env.PORT || 4000;
app.listen(PORT, () => {
  console.log(`StockSense backend running on http://localhost:${PORT}`);
});
