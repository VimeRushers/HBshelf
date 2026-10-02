# Stage 1: LibOrbisPkg's PkgTool (MIT, github.com/maxton/LibOrbisPkg) retargeted to .NET 8.
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS pkgtool
ARG LIBORBISPKG_REF=643477263b2644e0803e0f58b8726ea4e3f3b7d4
RUN git init -q /src && cd /src \
 && git remote add origin https://github.com/maxton/LibOrbisPkg \
 && git fetch -q --depth 1 origin ${LIBORBISPKG_REF} \
 && git checkout -q FETCH_HEAD
WORKDIR /src
RUN sed -i 's#<TargetFramework>[^<]*</TargetFramework>#<TargetFramework>net8.0</TargetFramework>#' \
      PkgTool.Core/PkgTool.Core.csproj LibOrbisPkg.Core/LibOrbisPkg.Core.csproj \
 && dotnet publish PkgTool.Core/PkgTool.Core.csproj -c Release -o /out -p:RollForward=Major

# Stage 2: the app (Python) on the .NET runtime image so PkgTool can run.
FROM mcr.microsoft.com/dotnet/runtime:8.0
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 PYTHONUNBUFFERED=1 PATH=/venv/bin:$PATH
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv unar aria2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN python3 -m venv /venv && pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=pkgtool /out /opt/pkgtool
COPY shelf /app/shelf
COPY tests /app/tests
WORKDIR /app
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8420/healthz')"
CMD ["uvicorn", "shelf.main:app", "--host", "0.0.0.0", "--port", "8420", "--log-level", "info"]
