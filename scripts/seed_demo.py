import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Product, User
from app.schemas import ProductInput
from app.security import hash_password
from app.services.catalog import create_product
from app.services.vector_store import sync_pending_catalog


PRODUCTS = [
    {
        "title": "Agentic Workflows with LangGraph", "slug": "agentic-workflows-langgraph", "category": "Agentic AI", "level": "Advanced", "price": 219,
        "skills": ["LangGraph", "checkpointing", "tool use", "recovery"], "outcomes": ["Build a stateful agent graph", "Add durable recovery", "Trace every workflow node"],
        "description": "Compose reliable multi-step agents with explicit state, tools, checkpoints, failure recovery, and production-grade evaluation.", "duration_minutes": 720,
    },
    {
        "title": "Building Production RAG Systems", "slug": "production-rag-systems", "category": "Generative AI", "level": "Advanced", "price": 189,
        "skills": ["RAG", "Qdrant", "reranking", "evaluation"], "outcomes": ["Build hybrid retrieval", "Measure grounding", "Recover from index drift"],
        "description": "Ship retrieval that holds up under real traffic with hybrid search, metadata filters, reranking, evaluation sets, and traceable evidence.", "duration_minutes": 600,
    },
    {
        "title": "Python for AI Engineers", "slug": "python-for-ai-engineers", "category": "Python for AI", "level": "Intermediate", "price": 129,
        "skills": ["Python", "asyncio", "Pydantic", "testing"], "outcomes": ["Write typed AI services", "Test LLM integrations", "Build reliable async pipelines"],
        "description": "Typing, async, data models, packaging, and testing refactored around the patterns used in real AI application codebases.", "duration_minutes": 540,
    },
    {
        "title": "Agentic AI Bootcamp", "slug": "agentic-ai-bootcamp", "category": "Agentic AI", "level": "Advanced", "price": 499,
        "skills": ["planning", "browser agents", "evaluation", "deployment"], "outcomes": ["Design an agent", "Evaluate tool trajectories", "Deploy a capstone system"],
        "description": "A cohort-style build track covering planning, tool use, browser and code agents, evaluation, and production deployment.", "duration_minutes": 1800,
    },
    {
        "title": "MLOps for Real Teams", "slug": "mlops-for-real-teams", "category": "MLOps", "level": "Intermediate", "price": 179,
        "skills": ["CI/CD", "model registry", "monitoring", "rollback"], "outcomes": ["Ship repeatable pipelines", "Monitor model quality", "Practice safe rollback"],
        "description": "The pipeline, review, release, observability, and rollback practices mature machine learning teams use every day.", "duration_minutes": 660,
    },
    {
        "title": "Streaming Data Engineering", "slug": "streaming-data-engineering", "category": "Data Engineering", "level": "Advanced", "price": 209,
        "skills": ["Kafka", "stream processing", "event schemas", "idempotency"], "outcomes": ["Design event contracts", "Process behavioral streams", "Handle late data"],
        "description": "Design durable event streams, schema evolution, idempotent consumers, and low-latency behavioral feature pipelines.", "duration_minutes": 780,
    },
    {
        "title": "Prompt Engineering to Production", "slug": "prompt-engineering-production", "category": "Generative AI", "level": "Intermediate", "price": 149,
        "skills": ["prompt design", "structured output", "evaluation", "versioning"], "outcomes": ["Version prompts", "Enforce output schemas", "Build regression suites"],
        "description": "Move from clever prompts to reliable, tested, versioned model behavior with structured outputs and measurable evaluations.", "duration_minutes": 420,
    },
    {
        "title": "Cloud and DevOps for AI Workloads", "slug": "cloud-devops-ai", "category": "Cloud & DevOps", "level": "Intermediate", "price": 169,
        "skills": ["containers", "secrets", "queues", "observability"], "outcomes": ["Containerize AI services", "Protect credentials", "Operate background workers"],
        "description": "Build and operate secure AI services with containers, queues, managed secrets, tracing, cost limits, and reliable deployments.", "duration_minutes": 570,
    },
    {
        "title": "Java Development Foundations", "slug": "java-development-foundations", "category": "Java Development", "level": "Beginner", "price": 99,
        "skills": ["Java", "object-oriented programming", "collections", "testing"], "outcomes": ["Build a complete Java application", "Model clean object-oriented domains", "Test and package Java projects"],
        "description": "Start with Java syntax and progress through object-oriented design, collections, error handling, testing, and a complete practical application.", "duration_minutes": 600,
    },
    {
        "title": "Spring Boot Web Services", "slug": "spring-boot-web-services", "category": "Java Development", "level": "Intermediate", "price": 149,
        "skills": ["Spring Boot", "REST APIs", "JPA", "security"], "outcomes": ["Create production-ready REST services", "Persist data safely", "Secure a Java web API"],
        "description": "Build reliable Java web services with Spring Boot, RESTful APIs, persistence, validation, authentication, testing, and deployment patterns.", "duration_minutes": 720,
    },
    {
        "title": "Scala for Data Engineering", "slug": "scala-data-engineering", "category": "Scala Development", "level": "Intermediate", "price": 159,
        "skills": ["Scala", "functional programming", "Spark", "data pipelines"], "outcomes": ["Write expressive Scala programs", "Process data with Spark", "Build typed data pipelines"],
        "description": "Learn practical Scala through functional programming, collections, type-safe design, Apache Spark, and scalable data engineering projects.", "duration_minutes": 690,
    },
    {
        "title": "Modern Web Technologies", "slug": "modern-web-technologies", "category": "Web Technologies", "level": "Beginner", "price": 109,
        "skills": ["HTML", "CSS", "JavaScript", "web accessibility"], "outcomes": ["Build responsive web interfaces", "Add accessible interactions", "Publish a complete web project"],
        "description": "Create polished responsive websites with semantic HTML, modern CSS, JavaScript, accessibility, browser APIs, and practical deployment workflows.", "duration_minutes": 540,
    },
    {
        "title": "Python Zero to Hero", "slug": "python-zero-to-hero", "category": "Python Development", "level": "Beginner", "price": 89,
        "skills": ["Python", "data structures", "automation", "APIs"], "outcomes": ["Write confident Python programs", "Automate everyday tasks", "Build and consume web APIs"],
        "description": "Move from your first Python program to useful automation, data processing, APIs, testing, and portfolio-ready projects with guided practice.", "duration_minutes": 780,
    },
    {
        "title": "MLOps Foundations", "slug": "mlops-foundations", "category": "MLOps", "level": "Beginner", "price": 119,
        "skills": ["experiments", "model packaging", "CI/CD", "monitoring"], "outcomes": ["Track reproducible experiments", "Package a model service", "Monitor a deployed model"],
        "description": "Learn the complete machine learning operations lifecycle from reproducible experiments and packaging to deployment, monitoring, and safe updates.", "duration_minutes": 510,
    },
    {
        "title": "LLM Foundations", "slug": "llm-foundations", "category": "Large Language Models", "level": "Beginner", "price": 119,
        "skills": ["transformers", "tokenization", "prompting", "evaluation"], "outcomes": ["Explain how modern LLMs work", "Design reliable prompts", "Evaluate model responses"],
        "description": "Understand tokens, transformers, prompting, context windows, safety, and evaluation through approachable examples and hands-on model experiments.", "duration_minutes": 480,
    },
    {
        "title": "Advanced LLM Application Engineering", "slug": "advanced-llm-application-engineering", "category": "Large Language Models", "level": "Advanced", "price": 229,
        "skills": ["structured output", "tool calling", "RAG", "LLM evaluation"], "outcomes": ["Build grounded LLM applications", "Add reliable tool use", "Operate evaluation-driven releases"],
        "description": "Engineer production LLM applications with structured outputs, tool calling, retrieval, guardrails, caching, observability, and rigorous evaluation.", "duration_minutes": 840,
    },
]

PRODUCTS.extend([
    {"title": "Multi-Agent Systems Design", "slug": "multi-agent-systems-design", "category": "Agentic AI", "level": "Advanced", "price": 239, "skills": ["multi-agent coordination", "planning", "memory", "evaluation"], "outcomes": ["Design collaborating agent roles", "Control shared state", "Evaluate coordinated workflows"], "description": "Design dependable multi-agent applications with clear roles, shared memory, coordination protocols, evaluation, and failure containment.", "duration_minutes": 780},
    {"title": "AI Agent Evaluation and Guardrails", "slug": "agent-evaluation-guardrails", "category": "Agentic AI", "level": "Intermediate", "price": 179, "skills": ["agent evaluation", "guardrails", "red teaming", "tracing"], "outcomes": ["Create trajectory evaluations", "Test unsafe tool behavior", "Add measurable release gates"], "description": "Measure agent decisions, tool trajectories, safety boundaries, and recovery behavior with practical evaluation suites and production guardrails.", "duration_minutes": 570},
    {"title": "Tool-Using Agents in Production", "slug": "tool-using-agents-production", "category": "Agentic AI", "level": "Advanced", "price": 229, "skills": ["tool calling", "MCP", "permissions", "resilience"], "outcomes": ["Build governed agent tools", "Enforce permissions", "Recover from tool failures"], "description": "Build production agents that call governed tools safely using structured contracts, scoped permissions, retries, idempotency, and operational telemetry.", "duration_minutes": 690},
    {"title": "Generative AI Product Design", "slug": "generative-ai-product-design", "category": "Generative AI", "level": "Beginner", "price": 119, "skills": ["AI product strategy", "prototyping", "UX", "evaluation"], "outcomes": ["Define a valuable AI use case", "Prototype a guided experience", "Measure user value"], "description": "Turn generative AI capabilities into clear product experiences through discovery, prototyping, human-centered interaction design, and measurable outcomes.", "duration_minutes": 420},
    {"title": "Multimodal AI Applications", "slug": "multimodal-ai-applications", "category": "Generative AI", "level": "Intermediate", "price": 189, "skills": ["vision models", "audio", "document AI", "evaluation"], "outcomes": ["Process image and text inputs", "Build a multimodal workflow", "Evaluate cross-modal quality"], "description": "Create applications that reason over text, images, audio, and documents with practical pipelines, structured outputs, and multimodal evaluation.", "duration_minutes": 600},
    {"title": "Fine-Tuning Language Models", "slug": "fine-tuning-language-models", "category": "Generative AI", "level": "Advanced", "price": 249, "skills": ["supervised fine-tuning", "LoRA", "datasets", "evaluation"], "outcomes": ["Prepare a tuning dataset", "Run parameter-efficient training", "Compare tuned model quality"], "description": "Prepare high-quality datasets and fine-tune language models with LoRA, reproducible experiments, safety review, and rigorous baseline comparison.", "duration_minutes": 810},
    {"title": "NumPy and Pandas for Machine Learning", "slug": "numpy-pandas-machine-learning", "category": "Python for AI", "level": "Beginner", "price": 99, "skills": ["NumPy", "Pandas", "data cleaning", "feature engineering"], "outcomes": ["Transform real datasets", "Create model-ready features", "Diagnose data quality"], "description": "Build confident numerical and tabular data skills using NumPy and Pandas through realistic cleaning, analysis, and feature-engineering projects.", "duration_minutes": 480},
    {"title": "FastAPI for AI Services", "slug": "fastapi-ai-services", "category": "Python for AI", "level": "Intermediate", "price": 149, "skills": ["FastAPI", "async Python", "validation", "deployment"], "outcomes": ["Expose a model API", "Validate structured requests", "Operate an asynchronous service"], "description": "Package models and AI workflows behind secure FastAPI services with typed contracts, asynchronous execution, testing, and deployment readiness.", "duration_minutes": 540},
    {"title": "Testing Python AI Systems", "slug": "testing-python-ai-systems", "category": "Python for AI", "level": "Advanced", "price": 169, "skills": ["pytest", "LLM testing", "mocking", "evaluation"], "outcomes": ["Test probabilistic behavior", "Mock external model calls", "Build reliable regression gates"], "description": "Test Python AI applications across deterministic code, external model integrations, probabilistic outputs, evaluation datasets, and failure recovery.", "duration_minutes": 510},
    {"title": "ML Model Monitoring", "slug": "ml-model-monitoring", "category": "MLOps", "level": "Intermediate", "price": 159, "skills": ["drift detection", "metrics", "alerts", "incident response"], "outcomes": ["Detect model drift", "Design useful alerts", "Investigate prediction incidents"], "description": "Monitor deployed models for data drift, prediction quality, latency, and operational failures with actionable alerts and incident workflows.", "duration_minutes": 510},
    {"title": "Feature Stores and ML Pipelines", "slug": "feature-stores-ml-pipelines", "category": "MLOps", "level": "Advanced", "price": 199, "skills": ["feature stores", "pipelines", "lineage", "point-in-time joins"], "outcomes": ["Design reusable features", "Prevent training skew", "Track feature lineage"], "description": "Design reusable batch and streaming features with point-in-time correctness, lineage, validation, and reliable training-serving consistency.", "duration_minutes": 660},
    {"title": "Kubernetes for ML Platforms", "slug": "kubernetes-ml-platforms", "category": "MLOps", "level": "Advanced", "price": 219, "skills": ["Kubernetes", "GPU scheduling", "model serving", "autoscaling"], "outcomes": ["Deploy model workloads", "Scale inference safely", "Operate shared ML infrastructure"], "description": "Operate machine-learning workloads on Kubernetes with model serving, resource controls, GPU scheduling, autoscaling, secrets, and safe releases.", "duration_minutes": 720},
    {"title": "SQL for Analytics Engineering", "slug": "sql-analytics-engineering", "category": "Data Engineering", "level": "Beginner", "price": 99, "skills": ["SQL", "data modeling", "analytics", "testing"], "outcomes": ["Write analytical SQL", "Model trustworthy datasets", "Test business transformations"], "description": "Learn analytical SQL, dimensional modeling, reusable transformations, data tests, and documentation through an end-to-end analytics project.", "duration_minutes": 510},
    {"title": "Apache Spark at Scale", "slug": "apache-spark-at-scale", "category": "Data Engineering", "level": "Advanced", "price": 209, "skills": ["Apache Spark", "optimization", "partitioning", "lakehouse"], "outcomes": ["Optimize distributed jobs", "Design efficient partitions", "Diagnose Spark performance"], "description": "Process large datasets with Apache Spark while mastering partitioning, query plans, memory behavior, lakehouse patterns, and performance diagnosis.", "duration_minutes": 750},
    {"title": "Modern Data Warehousing", "slug": "modern-data-warehousing", "category": "Data Engineering", "level": "Intermediate", "price": 169, "skills": ["warehousing", "dbt", "ELT", "dimensional modeling"], "outcomes": ["Design a warehouse model", "Build tested ELT workflows", "Serve reliable analytics"], "description": "Build a modern cloud warehouse with ELT pipelines, dimensional models, dbt-style transformations, testing, documentation, and governed access.", "duration_minutes": 630},
    {"title": "Data Quality and Observability", "slug": "data-quality-observability", "category": "Data Engineering", "level": "Intermediate", "price": 159, "skills": ["data contracts", "lineage", "quality tests", "incident response"], "outcomes": ["Create data contracts", "Detect broken pipelines", "Trace data incidents"], "description": "Protect critical data products with contracts, freshness checks, lineage, anomaly detection, ownership, and effective incident response practices.", "duration_minutes": 540},
    {"title": "Docker and Kubernetes Foundations", "slug": "docker-kubernetes-foundations", "category": "Cloud & DevOps", "level": "Beginner", "price": 109, "skills": ["Docker", "Kubernetes", "networking", "deployment"], "outcomes": ["Containerize an application", "Deploy to Kubernetes", "Debug common cluster issues"], "description": "Move from local containers to a working Kubernetes deployment while learning images, networking, configuration, storage, and troubleshooting.", "duration_minutes": 570},
    {"title": "Terraform Cloud Infrastructure", "slug": "terraform-cloud-infrastructure", "category": "Cloud & DevOps", "level": "Intermediate", "price": 159, "skills": ["Terraform", "infrastructure as code", "state", "security"], "outcomes": ["Provision repeatable infrastructure", "Manage Terraform state", "Review secure cloud changes"], "description": "Provision reproducible cloud infrastructure with Terraform modules, remote state, policy checks, secret handling, reviews, and safe change workflows.", "duration_minutes": 600},
    {"title": "Production Observability and SRE", "slug": "production-observability-sre", "category": "Cloud & DevOps", "level": "Advanced", "price": 199, "skills": ["SRE", "tracing", "SLIs", "incident management"], "outcomes": ["Define meaningful service levels", "Trace distributed failures", "Run effective incidents"], "description": "Operate reliable services using metrics, logs, traces, service-level objectives, capacity planning, alert design, and incident learning.", "duration_minutes": 690},
    {"title": "Java Microservices Architecture", "slug": "java-microservices-architecture", "category": "Java Development", "level": "Advanced", "price": 199, "skills": ["Java", "microservices", "messaging", "resilience"], "outcomes": ["Design service boundaries", "Implement resilient communication", "Test distributed workflows"], "description": "Design production Java microservices with clear domain boundaries, messaging, data ownership, resilience patterns, observability, and contract testing.", "duration_minutes": 750},
    {"title": "Reactive Java with Spring", "slug": "reactive-java-spring", "category": "Java Development", "level": "Advanced", "price": 189, "skills": ["Spring WebFlux", "Reactor", "backpressure", "testing"], "outcomes": ["Build reactive APIs", "Control backpressure", "Test asynchronous flows"], "description": "Build responsive Java services with Spring WebFlux, Reactor, non-blocking data access, backpressure, testing, and production diagnostics.", "duration_minutes": 660},
    {"title": "Functional Scala Foundations", "slug": "functional-scala-foundations", "category": "Scala Development", "level": "Beginner", "price": 119, "skills": ["Scala", "functional programming", "types", "collections"], "outcomes": ["Write idiomatic Scala", "Model domains with types", "Compose functional programs"], "description": "Learn Scala syntax and functional programming through immutable data, expressive types, collections, error handling, and practical applications.", "duration_minutes": 540},
    {"title": "Apache Spark with Scala", "slug": "apache-spark-scala", "category": "Scala Development", "level": "Advanced", "price": 199, "skills": ["Scala", "Spark", "distributed data", "optimization"], "outcomes": ["Build typed Spark jobs", "Optimize distributed transformations", "Test data pipelines"], "description": "Build type-safe Apache Spark pipelines in Scala with datasets, structured streaming, performance tuning, testing, and deployment workflows.", "duration_minutes": 720},
    {"title": "Typelevel Scala Services", "slug": "typelevel-scala-services", "category": "Scala Development", "level": "Advanced", "price": 209, "skills": ["Cats Effect", "HTTP services", "concurrency", "testing"], "outcomes": ["Build functional services", "Control concurrent effects", "Test resource-safe programs"], "description": "Create resource-safe Scala services with Cats Effect, typed HTTP APIs, functional concurrency, streaming, observability, and disciplined testing.", "duration_minutes": 690},
    {"title": "React Application Engineering", "slug": "react-application-engineering", "category": "Web Technologies", "level": "Intermediate", "price": 149, "skills": ["React", "state management", "testing", "accessibility"], "outcomes": ["Build a scalable React UI", "Manage application state", "Test accessible interactions"], "description": "Engineer maintainable React applications with component architecture, state management, routing, data fetching, testing, and accessibility.", "duration_minutes": 600},
    {"title": "TypeScript Full-Stack Development", "slug": "typescript-full-stack-development", "category": "Web Technologies", "level": "Intermediate", "price": 169, "skills": ["TypeScript", "Node.js", "web APIs", "databases"], "outcomes": ["Build a typed web application", "Design secure APIs", "Share contracts across the stack"], "description": "Build a complete TypeScript product with a modern frontend, secure Node.js APIs, relational data, validation, testing, and deployment.", "duration_minutes": 720},
    {"title": "Web Performance and Accessibility", "slug": "web-performance-accessibility", "category": "Web Technologies", "level": "Advanced", "price": 139, "skills": ["Core Web Vitals", "accessibility", "profiling", "progressive enhancement"], "outcomes": ["Improve page performance", "Audit accessible experiences", "Build resilient interfaces"], "description": "Make web products fast and inclusive through performance profiling, Core Web Vitals, semantic design, assistive-technology testing, and progressive enhancement.", "duration_minutes": 480},
    {"title": "Advanced Python Engineering", "slug": "advanced-python-engineering", "category": "Python Development", "level": "Advanced", "price": 179, "skills": ["Python", "architecture", "concurrency", "performance"], "outcomes": ["Design maintainable packages", "Choose safe concurrency models", "Profile Python systems"], "description": "Master advanced Python architecture, typing, concurrency, packaging, profiling, memory behavior, testing strategy, and maintainable service design.", "duration_minutes": 690},
    {"title": "Django Production Web Apps", "slug": "django-production-web-apps", "category": "Python Development", "level": "Intermediate", "price": 159, "skills": ["Django", "PostgreSQL", "authentication", "deployment"], "outcomes": ["Build a secure Django product", "Model relational data", "Deploy and operate the application"], "description": "Create a production Django application with relational modeling, authentication, permissions, APIs, testing, caching, and reliable deployment.", "duration_minutes": 660},
    {"title": "Python Automation and APIs", "slug": "python-automation-apis", "category": "Python Development", "level": "Intermediate", "price": 119, "skills": ["automation", "REST APIs", "scheduling", "error handling"], "outcomes": ["Automate repeatable workflows", "Integrate external APIs", "Schedule resilient jobs"], "description": "Automate practical business workflows with Python, REST APIs, files, scheduling, robust error handling, logging, and reusable command-line tools.", "duration_minutes": 510},
    {"title": "LLM Evaluation and Red Teaming", "slug": "llm-evaluation-red-teaming", "category": "Large Language Models", "level": "Advanced", "price": 219, "skills": ["LLM evaluation", "red teaming", "datasets", "safety"], "outcomes": ["Build an evaluation dataset", "Test model failure modes", "Create quality release gates"], "description": "Evaluate language-model quality, grounding, safety, and robustness with representative datasets, automated graders, human review, and red-team exercises.", "duration_minutes": 690},
    {"title": "LLMOps Deployment and Monitoring", "slug": "llmops-deployment-monitoring", "category": "Large Language Models", "level": "Advanced", "price": 229, "skills": ["LLMOps", "model gateways", "monitoring", "cost control"], "outcomes": ["Deploy a governed LLM service", "Monitor quality and cost", "Implement safe provider fallback"], "description": "Deploy and operate language-model applications with gateways, routing, caching, prompt releases, quality monitoring, budgets, and provider fallbacks.", "duration_minutes": 750},
    {"title": "Small Language Models on Device", "slug": "small-language-models-device", "category": "Large Language Models", "level": "Intermediate", "price": 189, "skills": ["small language models", "quantization", "edge inference", "benchmarking"], "outcomes": ["Select an efficient model", "Quantize for local inference", "Benchmark device performance"], "description": "Run capable small language models locally through model selection, quantization, constrained inference, benchmarking, privacy, and product integration.", "duration_minutes": 600},
    {"title": "Advanced Prompt and Context Engineering", "slug": "advanced-prompt-context-engineering", "category": "Large Language Models", "level": "Advanced", "price": 189, "skills": ["context engineering", "prompt design", "memory", "evaluation"], "outcomes": ["Design reliable context pipelines", "Control long conversations", "Evaluate prompt behavior"], "description": "Engineer reliable model context with structured prompts, memory policies, retrieval composition, long-context strategies, caching, and behavioral evaluation.", "duration_minutes": 630},
])

assert len(PRODUCTS) == 50, "The demo catalog must contain exactly 50 courses"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-password")
    parser.add_argument("--user-password", default="DemoUser123!")
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        if not db.scalar(select(User).where(User.email == "admin@smartreco.local")):
            if not args.admin_password:
                raise SystemExit("--admin-password is required when creating the demo admin for the first time")
            db.add(User(email="admin@smartreco.local", display_name="SmartReco Admin", password_hash=hash_password(args.admin_password), role="admin"))
        if not db.scalar(select(User).where(User.email == "learner@smartreco.local")):
            db.add(User(email="learner@smartreco.local", display_name="Avery Learner", password_hash=hash_password(args.user_password), role="user"))
        for index, payload in enumerate(PRODUCTS):
            if db.scalar(select(Product).where(Product.slug == payload["slug"])):
                continue
            data = ProductInput(**payload, currency="USD", status="active", rating=round(4.5 + (index % 5) * 0.1, 1), popularity=max(100, 900 - index * 50))
            create_product(db, data)
        db.commit()
    finally:
        db.close()
    print(sync_pending_catalog(limit=100))
    print("Seeded admin@smartreco.local and learner@smartreco.local")


if __name__ == "__main__":
    main()
