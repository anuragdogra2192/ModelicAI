"""Shared toolbox for ModelicAI Studio.

Creates the single OpenModelica session and the agents once, at startup. The Manager Agent
(agents/manager.py) decides what to do and calls these parts through its tools:

  client        OpenModelicaClient  - one OMC session (guarded by api.services.omc_lock)
  designer      DesignAgent         - writes / edits Modelica code (used by the design subgraph)
  sim_agent     SimulationAgent     - proposes and checks simulation settings
  graph_parser  ModelicaGraphParser - writes graph_schema.json (simplified diagram fallback)

The earlier one-shot pipeline (action planner, run loop, shared state, Plotly dashboard) was
replaced by the Manager graph and removed.
"""
from agents.designer import DesignAgent
from agents.graph_parser import ModelicaGraphParser
from agents.simulation import SimulationAgent
from utils.omc_client import OpenModelicaClient

# Each model gets its own folder: models/<ModelName>/
MODEL_ROOT = "models"


class SimulationOrchestrator:
  def __init__(self):
    self.client = OpenModelicaClient()
    self.designer = DesignAgent()
    self.sim_agent = SimulationAgent()
    self.graph_parser = ModelicaGraphParser()
