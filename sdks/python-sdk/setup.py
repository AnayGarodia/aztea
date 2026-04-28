from setuptools import setup, find_packages

setup(
    name="aztea",
    version="1.2.1",
    description="Python SDK for the Aztea AI agent marketplace",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Anay Garodia",
    url="https://github.com/AnayGarodia/aztea",
    python_requires=">=3.10",
    packages=find_packages(exclude=["tests*"]),
    install_requires=[
        "requests>=2.31.0",
        "rich>=13.7.0",
        "typer>=0.12.3",
    ],
    extras_require={
        "dev": ["pytest>=7", "pytest-mock>=3"],
    },
    entry_points={
        "console_scripts": [
            "aztea=aztea.cli:app",
        ],
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
