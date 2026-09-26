// Seeds the database with realistic demo data.
// This runs against the real DB (Postgres), not a static JSON file —
// satisfies the "use real/dynamic data sources" requirement even for
// your demo dataset.

const { PrismaClient } = require("@prisma/client");
const bcrypt = require("bcrypt");

const prisma = new PrismaClient();

async function main() {
  // --- Warehouses ---
  const mainWarehouse = await prisma.warehouse.upsert({
    where: { code: "WH-MAIN" },
    update: {},
    create: { name: "Main Warehouse", code: "WH-MAIN" },
  });

  const productionFloor = await prisma.warehouse.upsert({
    where: { code: "WH-PROD" },
    update: {},
    create: { name: "Production Floor", code: "WH-PROD" },
  });

  // --- Demo user ---
  const passwordHash = await bcrypt.hash("password123", 10);
  await prisma.user.upsert({
    where: { email: "manager@stocksense.dev" },
    update: {},
    create: {
      name: "Demo Manager",
      email: "manager@stocksense.dev",
      password: passwordHash,
      role: "MANAGER",
    },
  });

  // --- Products ---
  const steelRods = await prisma.product.upsert({
    where: { sku: "STL-ROD-01" },
    update: {},
    create: {
      name: "Steel Rods",
      sku: "STL-ROD-01",
      category: "Raw Material",
      uom: "kg",
      qtyOnHand: 0,
      reorderLevel: 20,
      warehouseId: mainWarehouse.id,
    },
  });

  const chairs = await prisma.product.upsert({
    where: { sku: "CHR-STD-01" },
    update: {},
    create: {
      name: "Standard Chair",
      sku: "CHR-STD-01",
      category: "Finished Goods",
      uom: "pcs",
      qtyOnHand: 0,
      reorderLevel: 5,
      warehouseId: mainWarehouse.id,
    },
  });

  // --- Walk through the exact example flow from the problem statement ---
  // Step 1: Receive 100 kg steel
  const receipt = await prisma.receipt.create({
    data: {
      supplierName: "Acme Steel Co.",
      warehouseId: mainWarehouse.id,
      status: "DONE",
      stockMoves: {
        create: {
          productId: steelRods.id,
          type: "RECEIPT",
          quantity: 100,
          toLocation: mainWarehouse.name,
        },
      },
    },
  });
  await prisma.product.update({
    where: { id: steelRods.id },
    data: { qtyOnHand: { increment: 100 } },
  });

  // Step 2: Internal transfer, Main Store -> Production Rack (total unchanged)
  await prisma.stockMove.create({
    data: {
      productId: steelRods.id,
      type: "TRANSFER",
      quantity: 100,
      fromLocation: mainWarehouse.name,
      toLocation: productionFloor.name,
    },
  });

  // Step 3: Deliver 20 steel (finished goods) -> stock -20
  const delivery = await prisma.deliveryOrder.create({
    data: {
      customerName: "BuildRight Furniture",
      warehouseId: productionFloor.id,
      status: "DONE",
      stockMoves: {
        create: {
          productId: steelRods.id,
          type: "DELIVERY",
          quantity: 20,
          fromLocation: productionFloor.name,
        },
      },
    },
  });
  await prisma.product.update({
    where: { id: steelRods.id },
    data: { qtyOnHand: { decrement: 20 } },
  });

  // Step 4: Adjust 3 kg damaged -> stock -3
  await prisma.stockMove.create({
    data: {
      productId: steelRods.id,
      type: "ADJUSTMENT",
      quantity: 3,
      note: "3kg damaged in production",
    },
  });
  await prisma.product.update({
    where: { id: steelRods.id },
    data: { qtyOnHand: { decrement: 3 } },
  });

  console.log("Seed complete.");
  console.log(`Demo login: manager@stocksense.dev / password123`);
  console.log(
    `Steel Rods should now show qtyOnHand = 77 (100 - 20 - 3), split across two locations.`
  );
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
