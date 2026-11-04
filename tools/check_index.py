"""Quick quality check of data/msl_index.db against the real MSL.  Run:  docker exec -w /app modelicai python3 -m tools.check_index"""
import sqlite3
from agents import library as L

db = sqlite3.connect(L.INDEX_PATH)
q = lambda s: db.execute(s).fetchone()[0]
print("meta:", L.meta())
tot = q("SELECT COUNT(*) FROM components WHERE type NOT IN ('Real','Integer','Boolean','String')")
res = q("SELECT COUNT(*) FROM components WHERE type NOT IN ('Real','Integer','Boolean','String') AND type_id IS NOT NULL")
print(f"component types resolved: {res}/{tot} ({100*res/max(tot,1):.0f}%)")
print("connectors found:", q("SELECT COUNT(*) FROM components WHERE is_connector=1"),
      "| extends:", q("SELECT COUNT(*) FROM edges WHERE type='extends'"),
      "| replaceable:", q("SELECT COUNT(*) FROM edges WHERE type='replaceable'"),
      "| example uses:", q("SELECT COUNT(*) FROM edges WHERE type='uses'"))
print("\nsearch 'tank':", [r["id"] for r in L.search("tank", 4)])
print("search 'gear':", [r["id"] for r in L.search("gear", 4)])
print("search 'pump':", [r["id"] for r in L.search("pump", 4)])
print("\n" + L.describe_text("Modelica.Fluid.Machines.PrescribedPump"))
print("\n" + L.describe_text("Modelica.Fluid.Vessels.OpenTank"))
print("\nexamples(PrescribedPump, OpenTank):", [e["id"] for e in L.examples(["Modelica.Fluid.Machines.PrescribedPump", "Modelica.Fluid.Vessels.OpenTank"])])
bad = """model T
  parameter Modelica.SIunits.Area A = 1;
  Modelica.Fluid.Machines.PrescribedPump pump(redeclare package Medium = Modelica.Media.Water.ConstantPropertyLiquidWater, N_nom=1500);
  Modelica.Fluid.Vessels.OpenTank tank(redeclare package Medium = Modelica.Media.Water.ConstantPropertyLiquidWater, height=2, crossArea=1, nPorts=1);
equation
  connect(tank.port, pump.port_a);
end T;"""
print("\nprecheck(bad):"); [print("  -", i) for i in L.precheck(bad)]
good = """model G
  inner Modelica.Fluid.System system;
  replaceable package Medium = Modelica.Media.Water.ConstantPropertyLiquidWater;
  Modelica.Fluid.Vessels.OpenTank tank(redeclare package Medium = Medium, height=2, crossArea=1, level_start=1, nPorts=1, use_portsData=false);
  Modelica.Fluid.Machines.PrescribedPump pump(redeclare package Medium = Medium, N_nominal=1500, use_N_in=false,
    redeclare function flowCharacteristic = Modelica.Fluid.Machines.BaseClasses.PumpCharacteristics.linearFlow(V_flow_nominal={0, 0.01}, head_nominal={20, 0}));
  Modelica.Fluid.Sources.Boundary_pT sink(redeclare package Medium = Medium, nPorts=1);
  Modelica.Mechanics.Rotational.Components.Inertia inertia(J=1, w(start=0));
  Modelica.Mechanics.Rotational.Components.IdealGear gear(ratio=3);
  Modelica.Electrical.Analog.Basic.Resistor r(R=10);
  Modelica.Electrical.Analog.Basic.Ground g;
  Modelica.Thermal.HeatTransfer.Components.HeatCapacitor cap(C=1000, T(start=293.15));
  Modelica.Blocks.Continuous.TransferFunction tf(b={1}, a={2, 1});
equation
  connect(tank.ports[1], pump.port_a);
  connect(pump.port_b, sink.ports[1]);
  connect(inertia.flange_b, gear.flange_a);
  connect(r.n, g.p);
end G;"""
print("\nprecheck(valid model, should be empty):", L.precheck(good))
