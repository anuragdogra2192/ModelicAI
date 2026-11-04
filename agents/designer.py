import os
from langchain_core.messages import HumanMessage, SystemMessage
from agents.llm import make_llm

class DesignAgent:

  def __init__(self, model_name: str = "claude-haiku-5-5"):
    """Initializes the Design Agent with the configured LLM (agents/llm.py: Claude or Meta Muse)."""
    self.llm = make_llm("designer", max_tokens=8192)

  def generate_code(self, prompt: str, error_log: str = None, context: str = None) -> str:
    """Generates valid Modelica (.mo) syntax based on user instructions.

    If an error_log is provided from the compilation check, it executes the
    self-correction loop by feeding the traceback back to the model[cite: 1].
    """

    system_prompt = (
            "You are an expert Modelica code generation agent for ModelicAI Studio.\n"
            "You write valid, syntactically correct Modelica code (.mo).\n"
            "COMPONENT-FIRST RULE (most important):\n"
            "Build every model as a DIAGRAM: instantiate Modelica Standard Library (MSL 4.x) components and connect\n"
            "them with connect(). Do NOT write the physics as raw equations (der(x) = ...) when library components\n"
            "exist. Only write raw equations if the user explicitly asks for an equation-based model, or for a small\n"
            "custom law that no component covers (then wrap it in a Modelica.Blocks expression/source block).\n"
            "Use the simplest component set that fits. Pick from:\n"
            "- Electrical: Modelica.Electrical.Analog.Basic.{Resistor(R), Capacitor(C), Inductor(L), Ground, EMF(k)},\n"
            "  .Sources.{ConstantVoltage(V), StepVoltage, SineVoltage, ConstantCurrent}, .Sensors.{VoltageSensor, CurrentSensor}\n"
            "- Translational mechanics: Modelica.Mechanics.Translational.Components.{Mass(m), Spring(c), Damper(d),\n"
            "  SpringDamper(c,d), Fixed}, .Sources.{Force, ConstantForce, Position}, .Sensors.{PositionSensor, SpeedSensor}\n"
            "- Rotational mechanics: Modelica.Mechanics.Rotational.Components.{Inertia(J), Spring(c), Damper(d), IdealGear(ratio), Fixed},\n"
            "  .Sources.{Torque, ConstantTorque, Speed}, .Sensors.{SpeedSensor, AngleSensor}\n"
            "- Thermal: Modelica.Thermal.HeatTransfer.Components.{HeatCapacitor(C, T(start)), ThermalConductor(G), Convection},\n"
            "  .Sources.{FixedTemperature(T), PrescribedHeatFlow, FixedHeatFlow(Q_flow)}, .Sensors.TemperatureSensor\n"
            "- Fluid / tanks: Modelica.Fluid.Vessels.OpenTank(height, crossArea, level_start, nPorts, portsData),\n"
            "  Modelica.Fluid.Sources.{MassFlowSource_T(use_m_flow_in), Boundary_pT}, Modelica.Fluid.Pipes.StaticPipe,\n"
            "  Modelica.Fluid.Valves.ValveLinear, with `replaceable package Medium = Modelica.Media.Water.ConstantPropertyLiquidWater`\n"
            "  Simple pump: prefer MassFlowSource_T(use_m_flow_in=true) driven by a signal. Only if a pump curve is needed:\n"
            "  Modelica.Fluid.Machines.PrescribedPump(redeclare package Medium = Medium, N_nominal=1500, use_N_in=false,\n"
            "    redeclare function flowCharacteristic =\n"
            "      Modelica.Fluid.Machines.BaseClasses.PumpCharacteristics.linearFlow(V_flow_nominal={0, 0.01}, head_nominal={20, 0}))\n"
            "  (PrescribedPump has NO parameters N_nom, m_flow_nominal or V_flow_nominal; the curve carries the flow data.)\n"
            "- Units: MSL 4 uses Modelica.Units.SI (e.g. Modelica.Units.SI.Area). Modelica.SIunits does NOT exist.\n"
            "  and an `inner Modelica.Fluid.System system`. (For a purely conceptual level model, a\n"
            "  Modelica.Blocks.Continuous.Integrator fed by Blocks.Math.Add of inflow/outflow signals is acceptable.)\n"
            "- Control and signals: Modelica.Blocks.Sources.{Step, Ramp, Sine, Constant}, Modelica.Blocks.Continuous.{PID, LimPID,\n"
            "  Integrator, FirstOrder, TransferFunction}, Modelica.Blocks.Math.{Gain, Add, Feedback}\n"
            "Expose the key physical values as top-level parameters and bind component parameters to them (e.g. spring(c=k)).\n"
            "CRITICAL MODELICA RULES:\n"
            "1. Use fully qualified class names (Modelica.Electrical.Analog.Basic.Resistor) so the model loads without imports.\n"
            "2. Connect only compatible connectors (pins to pins, flanges to flanges, heat ports to heat ports,\n"
            "   fluid ports to fluid ports, RealOutput to RealInput); every physical connector must be connected.\n"
            "3. Keep equations balanced; give states a start value where it matters.\n"
            "4. Give EVERY component instance a diagram layout, like OMEdit would:\n"
            "   `annotation(Placement(transformation(origin={x,y}, extent={{-10,-10},{10,10}}, rotation=r)))`\n"
            "   on a 20-unit grid inside {{-100,-100},{100,100}}, signal/energy flowing left to right,\n"
            "   ground/fixed components below what they attach to, rotation in multiples of 90.\n"
            "5. Give every connect() a wire route: `connect(a.p, b.n) annotation(Line(points={{x1,y1},...,{xn,yn}}, color={0,0,255}));`\n"
            "   with orthogonal segments that start and end exactly on the connector positions.\n"
            "6. Add `annotation(Diagram(coordinateSystem(extent={{-100,-100},{100,100}})), experiment(StopTime=..))` with a\n"
            "   StopTime that shows the full dynamic response (about 5 time constants).\n"
            "7. Return ONLY raw Modelica code or code inside a clean ```modelica block. No conversational text."
        )

    user_content = f"User Request: {prompt}"
    if context:  # facts from the installed MSL (agents/library.py): exact class paths, parameters, connectors
      user_content += f"\n\n{context}"

    # ReAct Self-Correction Loop: Ingest compiler error tracebacks if validation fails
    if error_log:
      user_content += (
          f"\n\nPrevious compilation failed with this error log:\n{error_log}\n"
          "Please analyze this traceback, fix the syntax, balance the DAE equations, "
          "or correct the component definitions, and output the corrected Modelica code."
      )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    content, response = self._ask(messages)
    if not content.strip():
      # Empty answer (e.g. a reasoning model spent its whole output budget thinking): ask once more, briefly.
      meta = getattr(response, "response_metadata", {}) or {}
      self.last_note = f"finish_reason={meta.get('finish_reason') or meta.get('stop_reason')}, usage={meta.get('token_usage') or meta.get('usage')}"
      print(f"Designer: empty answer ({self.last_note}), retrying once")
      messages = messages + [HumanMessage(content="Your previous reply contained no Modelica code. Reply now with only "
                                                  "the complete model in one ```modelica code block, no explanation.")]
      content, response = self._ask(messages)
    return self._extract(content)

  def _ask(self, messages):
    response = self.llm.invoke(messages)
    content = response.content
    # Anthropic returns a list of content blocks; keep only the text parts.
    if isinstance(content, list):
      content = "".join(
          block.get("text", "") for block in content
          if isinstance(block, dict) and block.get("type") == "text"
      )

    return content or "", response

  @staticmethod
  def _extract(content: str) -> str:
    # Cleanly extract the code block if wrapped in markdown
    if "```modelica" in content:
      content = content.split("```modelica")[1].split("```")[0].strip()
    elif "```" in content:
      content = content.split("```")[1].split("```")[0].strip()

    return content