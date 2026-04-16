from setuptools import setup, find_packages

setup(
    name="agentmarket",
    version="1.0.0",
    description="Python SDK for the AgentMarket AI agent marketplace",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Anay Garodia",
    url="https://github.com/AnayGarodia/agentmarket",
    python_requires=">=3.10",
    packages=find_packages(exclude=["tests*"]),
    install_requires=[
        "httpx>=0.25",
        "pydantic>=2",
    ],
    extras_require={
        "dev": ["pytest>=7", "pytest-mock>=3"],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Internet :: WWW/HTTP",
    ],
    keywords="ai agent marketplace llm automation sdk",
    project_urls={
        "Documentation": "https://github.com/AnayGarodia/agentmarket/blob/main/docs/quickstart.md",
        "Source": "https://github.com/AnayGarodia/agentmarket",
        "Issues": "https://github.com/AnayGarodia/agentmarket/issues",
    },
)
