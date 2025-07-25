<script lang="ts">
	import type { PageData } from './$types';
	import { pageTitle } from '$lib/utils/stores';
	import { m } from '$paraglide/messages';
	interface Props {
		data: PageData;
	}

	let { data }: Props = $props();
	pageTitle.set('Visual Analysis');

	// Extract the x-axis values
	const xAxisPoints =
		data.data.length > 0
			? Object.keys(data.data[0].data)
					.map(Number)
					.sort((a, b) => a - b)
			: [];

	function formatTimePoint(seconds: number) {
		if (seconds === 0) return '0';

		const days = Math.floor(seconds / 86400);
		const hours = Math.floor((seconds % 86400) / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);

		const parts = [];
		if (days) parts.push(`${m['dayCount']({ count: days })}`);
		if (hours) parts.push(`${m['hourCount']({ count: hours })}`);
		if (minutes) parts.push(`${m['minuteCount']({ count: minutes })}`);

		return parts.join(' ');
	}

	// Helper to determine if a cell represents an impact change
	function isImpactChange(entry, currentIndex) {
		if (currentIndex === 0) return true;

		const currentPoint = xAxisPoints[currentIndex];
		const previousPoint = xAxisPoints[currentIndex - 1];

		return entry.data[currentPoint].value !== entry.data[previousPoint].value;
	}
</script>

<div class="bg-white shadow-sm flex overflow-x-auto">
	<div class="w-full">
		<table class="min-w-full border-collapse">
			<thead>
				<tr class="bg-gray-100">
					<th class="px-4 py-2 text-left font-medium text-gray-600">{m.asset()}</th>
					{#each xAxisPoints as point, i}
						<th class="px-4 py-2 text-center font-medium text-gray-600">
							T{i}
						</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each data.data as entry}
					<tr class="border-t border-gray-200">
						<td class="px-4 py-2 font-medium">{entry.folder}/{entry.asset}</td>
						{#each xAxisPoints as point, i}
							<td
								class="px-4 py-2 text-center"
								style="background-color: {entry.data[point].hexcolor || '#f9fafb'};
								       {!isImpactChange(entry, i) ? 'border-left: none;' : ''}"
							>
								{#if isImpactChange(entry, i)}
									<div class="font-medium">{entry.data[point].name || '--'}</div>
								{/if}
							</td>
						{/each}
					</tr>
				{/each}
			</tbody>
			<tfoot>
				<tr class="bg-gray-50 border-t-2 border-gray-200">
					<td class="px-4 py-2 font-medium text-gray-600 capitalize">{m.time()}</td>
					{#each xAxisPoints as point}
						<td class="px-4 py-2 text-center text-sm text-gray-600">
							{formatTimePoint(point)}
						</td>
					{/each}
				</tr>
			</tfoot>
		</table>
	</div>
</div>

<style>
	table {
		border-collapse: separate;
		border-spacing: 0;
		width: 100%;
		box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
		border-radius: 6px;
	}

	th,
	td {
		border-right: 1px solid rgba(0, 0, 0, 0.05);
	}

	th:last-child,
	td:last-child {
		border-right: none;
	}

	/* Add visual cues for impact transitions */
	td[style*='border-left: none'] {
		position: relative;
	}

	td[style*='border-left: none']::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 1px;
		background: rgba(0, 0, 0, 0.05);
		opacity: 0.3;
	}

	/* Footer styling */
	tfoot td {
		font-size: 0.85rem;
		padding-top: 0.75rem;
		padding-bottom: 0.75rem;
	}
</style>
