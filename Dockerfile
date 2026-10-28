FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Step 1: Install base system utilities and build tools
RUN apt-get update && apt-get install -y \
    software-properties-common \
    curl \
    gnupg \
    python3-pip \
    python3-dev \
    git \
    g++ \
    gfortran \
    make \
    && rm -rf /var/lib/apt/lists/*

# Step 2: Add official OpenModelica stable repository & secure key ring
RUN curl -fsSL https://build.openmodelica.org/apt/openmodelica.asc \
    | gpg --dearmor -o /usr/share/keyrings/openmodelica-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/openmodelica-keyring.gpg] https://build.openmodelica.org/apt jammy stable" \
    > /etc/apt/sources.list.d/openmodelica.list && \
    apt-get update && apt-get install -y \
    openmodelica \
    && rm -rf /var/lib/apt/lists/*

# Step 3: Set working directory inside container
WORKDIR /app

# Step 4: Copy and install Python dependencies (includes OMPython)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Step 5: Copy application source code
COPY . .

# Step 6: Run as a non-root user. OMC refuses to start its server as root.
RUN useradd --create-home --uid 1000 modelica && \
    chown -R modelica:modelica /app
USER modelica

# Step 7: Expose port for FastAPI / MCP Server
EXPOSE 8000

CMD ["python3", "main.py"]