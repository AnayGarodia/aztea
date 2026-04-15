from setuptools import setup, find_packages

setup(
    name="agentmarket",
    version="1.0.0",
    description="Python SDK for the AgentMarket AI agent marketplace",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.10",
    packages=find_packages(exclude=["tests*"]),
    install_requires=[
        "httpx>=0.25",
        "pydantic>=2",
        "fastapi>=0.100",
        "uvicorn>=0.20",
    ],
    extras_require={
        "dev": ["pytest>=7", "pytest-mock>=3"],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
