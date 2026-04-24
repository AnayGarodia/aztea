from setuptools import setup, find_packages

setup(
    name="aztea",
    version="1.0.0",
    description="Python SDK for the Aztea AI agent marketplace",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Anay Garodia",
    url="https://github.com/AnayGarodia/aztea",
    python_requires=">=3.10",
    packages=find_packages(exclude=["tests*"]),
    install_requires=[
        "httpx>=0.25",
        "pydantic>=2",
    ],
    extras_require={
        "dev": ["pytest>=7", "pytest-mock>=3"],
        "tui": ["aztea-tui>=0.1.0"],
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
        "Documentation": "https://github.com/AnayGarodia/aztea/blob/main/docs/quickstart.md",
        "Source": "https://github.com/AnayGarodia/aztea",
        "Issues": "https://github.com/AnayGarodia/aztea/issues",
    },
)
