import matplotlib.pyplot as plt

benchmarks = {
    "Graph-BFS": [291.69, 251.08, 170.17, 98.41, 80.89],
    "File Compression": [35.00, 35.00, 27.57, 23.51, 22.54],
    "Video Processing": [438.99, 353.08, 352.30, 350.04, 349.35],
    "Graph-Pagerank": [20.00, 20.00, 20.00, 20.00, 20.00],
    "nodejs-ocr-image": [62.00, 62.00, 43.40, 43.93, 41.68],
    "image-recognition": [784.00, 684.82, 636.11, 538.17, 534.00],
    "BERT": [1462.47, 1138.60],
    "dna-visualization": [30.00, 30.00, 30.00, 26.50, 19.03],
}

def compute_sharing_values(memory_peaks):
    first_peak = memory_peaks[0]
    x = list(range(1, len(memory_peaks) + 1))
    y = [first_peak - peak for peak in memory_peaks]
    return x, y

plt.figure(figsize=(11, 7))

for benchmark_name, memory_peaks in benchmarks.items():
    x, y = compute_sharing_values(memory_peaks)
    first_peak = memory_peaks[0]
    label = f"{benchmark_name} (memory.peak={first_peak:.2f} MB)"
    plt.plot(x, y, marker="o", label=label)

plt.xlabel("Number of containers")
plt.ylabel("Sharing (MB)")
plt.title("Sharing trend vs number of containers")
plt.xticks([1, 2, 3, 4, 5])
plt.ylim(bottom=0)
plt.grid(True)
plt.legend()
plt.tight_layout()

plt.savefig("all_benchmarks_sharing.png", dpi=300, bbox_inches="tight")
print("Saved: all_benchmarks_sharing.png")